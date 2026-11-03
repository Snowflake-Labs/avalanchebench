# Copyright 2026 Snowflake Inc.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import csv
import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from avalanche_bench.cli import score_to_paths
from avalanche_bench.cortex import CortexClient, CortexError, CortexResponse
from avalanche_bench.evaluation import (
    EvaluationError,
    evaluate,
    extract_judge_vote,
    load_rubric,
    report_section,
)

RUBRIC = [
    {
        "category": "Quality",
        "max_points": "5",
        "question": "First question?",
        "scoring_criteria": (
            "Perfect (5 pts): correct. Half (2 pts): partial. Zero (0 pts): wrong."
        ),
        "subcategory": "Facts",
        "target_answer": "Answer one.",
    },
    {
        "category": "Quality",
        "max_points": "5",
        "question": "Second question?",
        "scoring_criteria": (
            "Perfect (5 pts): correct. Half (2 pts): partial. Zero (0 pts): wrong."
        ),
        "subcategory": "Facts",
        "target_answer": "Answer two.",
    },
]
MODELS = ("anthropic-model", "openai-model", "google-model")


class FakeClient:
    def __init__(self, votes: list[str]) -> None:
        self.votes = list(votes)
        self.calls: list[tuple[str, str]] = []
        self.lock = threading.Lock()

    def complete(self, model: str, prompt: str) -> CortexResponse:
        with self.lock:
            self.calls.append((model, prompt))
            vote = self.votes.pop(0)
        return CortexResponse(
            text=vote,
            request_id=f"request-{len(self.calls)}",
            usage={"input_tokens": 10, "output_tokens": 2},
            latency_ms=12.5,
        )


def fixed_time() -> datetime:
    return datetime(2026, 9, 17, tzinfo=UTC)


def test_regression_median_and_missing_section_fallback_behavior() -> None:
    client = FakeClient(
        [
            '{"bucket":"top","justification":"correct"}',
            '{"bucket":"middle","justification":"mostly correct"}',
            '{"bucket":"top","justification":"correct"}',
            '{"bucket":"zero","justification":"not answered"}',
            '{"bucket":"zero","justification":"not answered"}',
            '{"bucket":"zero","justification":"not answered"}',
        ]
    )
    reward, details, run = evaluate(
        report="## Q1\nA supported answer.\n",
        rubric=RUBRIC,
        models=MODELS,
        client=client,
        task="ecommerce",
        hashes={"report_sha256": "a" * 64},
        now=fixed_time,
    )
    assert len(client.calls) == 6
    assert reward["total_points"] == 5
    assert reward["max_points"] == 10
    assert reward["reward"] == 0.5
    assert details["questions"][0]["score"] == 5
    assert details["questions"][1]["evaluation_method"] == (
        "snowflake_cortex_rest_committee"
    )
    assert details["questions"][1]["prompt_scope"] == "whole_report"
    assert details["questions"][1]["extraction"]["fallback_reason"] == (
        "no_question_marker"
    )
    assert run["catc"] == "not_calculated_for_judge_calls"
    assert len(run["calls"]) == 6


def test_invalid_vote_retries_then_fails_closed_with_audit() -> None:
    client = FakeClient(["not-json"] * 6)
    with pytest.raises(EvaluationError, match="incomplete committee") as captured:
        evaluate(
            report="## Q1\nAnswer.\n",
            rubric=RUBRIC[:1],
            models=MODELS,
            client=client,
            task="ecommerce",
            hashes={},
            sleep=lambda _seconds: None,
            now=fixed_time,
        )
    assert len(client.calls) == 6
    assert len(captured.value.audit["calls"]) == 6
    assert all(
        call["error_class"] == "invalid_judge_output"
        for call in captured.value.audit["calls"]
    )


def test_existing_parser_semantics_accept_wrapped_json() -> None:
    bucket, justification = extract_judge_vote(
        'Result: ```json\n{"bucket":"top","justification":"correct"}\n```',
        {"top": 5.0, "zero": 0.0},
    )
    assert (bucket, justification) == ("top", "correct")


@pytest.mark.parametrize(
    ("alias", "canonical"),
    [("perfect", "top"), ("half", "middle"), ("partial", "low")],
)
def test_rubric_bucket_aliases_are_accepted(alias: str, canonical: str) -> None:
    assert extract_judge_vote(
        json.dumps({"bucket": alias, "justification": "supported"}),
        {"top": 5.0, "middle": 2.0, "low": 1.0, "zero": 0.0},
    ) == (canonical, "supported")


def test_run_records_overridden_endpoint_host(monkeypatch) -> None:
    monkeypatch.setenv("CORTEX_REST_ENDPOINT", "https://judge.example.test/other")
    client = CortexClient(pat="test-pat")
    client.complete = FakeClient(['{"bucket":"top","justification":"ok"}'] * 3).complete
    _, _, run = evaluate(
        report="## Q1\nAnswer.\n",
        rubric=RUBRIC[:1],
        models=MODELS,
        client=client,
        task="ecommerce",
        hashes={},
        now=fixed_time,
    )
    assert run["endpoint_host"] == "judge.example.test"
    assert run["endpoint_hosts"] == ["judge.example.test"]
    assert run["endpoint_is_default"] is False
    assert run["token_type"] == "PROGRAMMATIC_ACCESS_TOKEN"


def test_failed_rerun_removes_all_stale_score_outputs(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    task = root / "tasks/ecommerce"
    (task / "tests").mkdir(parents=True)
    (root / "data").mkdir()
    (root / "src/avalanche_bench").mkdir(parents=True)
    (task / "instruction.md").write_text("instruction\n")
    (root / "src/avalanche_bench/evaluation.py").write_text("source\n")
    (root / "src/avalanche_bench/extraction.py").write_text("extractor\n")
    (root / "src/avalanche_bench/cortex.py").write_text("transport\n")
    (root / "data/manifest.json").write_text(
        json.dumps({"archives": {"ecommerce": {"sha256": "a" * 64}}})
    )
    with (task / "tests/rubric.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=RUBRIC[0])
        writer.writeheader()
        writer.writerow(RUBRIC[0])
    report = tmp_path / "report.md"
    report.write_text("## Q1\nAnswer.\n")
    outputs = [tmp_path / name for name in ("reward.json", "details.json", "run.json")]
    for output in outputs:
        output.write_text("stale")
    with pytest.raises(EvaluationError):
        score_to_paths(
            task="ecommerce",
            report_path=report,
            reward_path=outputs[0],
            details_path=outputs[1],
            run_path=outputs[2],
            root=root,
            client=FakeClient(["invalid"]),
        )
    assert all(not output.exists() for output in outputs)


def test_report_section_uses_exact_question_headings() -> None:
    report = "# Report\n## Q1 - First\nOne\n## **Q2**\nTwo\n"
    assert report_section(report, 1) == "One"
    assert report_section(report, 2) == "Two"


def test_all_published_task_rubrics_load_with_metadata_columns() -> None:
    root = Path(__file__).parents[1]
    expected = {"ecommerce": 15, "customer-support": 25, "travel": 18}
    for task, row_count in expected.items():
        rubric = load_rubric(root / "tasks" / task / "tests/rubric.csv")
        assert len(rubric) == row_count


def test_heading_only_change_preserves_semantics_and_score() -> None:
    outcomes = []
    for heading in ("## Q1", "### Q1"):
        client = FakeClient(['{"bucket":"top","justification":"correct"}'] * 3)
        reward, details, _run = evaluate(
            report=f"{heading}\nSame semantic answer.\n",
            rubric=RUBRIC[:1],
            models=MODELS,
            client=client,
            task="synthetic",
            hashes={},
        )
        outcomes.append((reward, details))
    assert outcomes[0][0]["reward"] == outcomes[1][0]["reward"] == 1.0
    assert (
        outcomes[0][1]["questions"][0]["extraction"]["extracted_span_sha256"]
        == outcomes[1][1]["questions"][0]["extraction"]["extracted_span_sha256"]
    )


def test_missing_section_uses_whole_report_instead_of_zero() -> None:
    client = FakeClient(['{"bucket":"top","justification":"found elsewhere"}'] * 3)
    reward, details, _run = evaluate(
        report="A real answer without a recognized marker.",
        rubric=RUBRIC[:1],
        models=MODELS,
        client=client,
        task="synthetic",
        hashes={},
    )
    question = details["questions"][0]
    assert reward["reward"] == 1.0
    assert question["prompt_scope"] == "whole_report"
    assert question["extraction"]["fallback_reason"] == "no_question_marker"
    assert question["evaluation_method"] != "missing_section"
    assert all("FULL REPORT" in prompt for _, prompt in client.calls)


def test_duplicate_section_uses_whole_report_and_preserves_candidates() -> None:
    client = FakeClient(['{"bucket":"zero","justification":"ambiguous"}'] * 3)
    _reward, details, _run = evaluate(
        report="## Q1\nFirst.\n### Q1\nSecond.\n",
        rubric=RUBRIC[:1],
        models=MODELS,
        client=client,
        task="synthetic",
        hashes={},
    )
    extraction = details["questions"][0]["extraction"]
    assert extraction["fallback_reason"] == "duplicate_question_markers"
    assert extraction["final_prompt_scope"] == "whole_report"
    assert len(extraction["duplicate_candidates"]) == 2


def test_whitespace_only_report_is_the_only_no_call_zero() -> None:
    client = FakeClient([])
    reward, details, run = evaluate(
        report=" \n\t",
        rubric=RUBRIC[:1],
        models=MODELS,
        client=client,
        task="synthetic",
        hashes={},
    )
    assert reward["reward"] == 0.0
    assert details["questions"][0]["evaluation_method"] == "empty_report"
    assert details["questions"][0]["extraction"]["confidence"] == "certain_absent"
    assert run["calls"] == []
    assert client.calls == []


class ConcurrentState:
    def __init__(self) -> None:
        self.barrier = threading.Barrier(3)
        self.lock = threading.Lock()
        self.intervals: dict[str, tuple[float, float]] = {}
        self.client_count = 0
        self.closed_client_count = 0

    def factory(self) -> "ConcurrentClient":
        with self.lock:
            self.client_count += 1
        return ConcurrentClient(self)


class ConcurrentClient:
    def __init__(self, state: ConcurrentState) -> None:
        self.state = state

    def complete(self, model: str, _prompt: str) -> CortexResponse:
        started = time.monotonic()
        self.state.barrier.wait(timeout=1)
        time.sleep(0.02)
        ended = time.monotonic()
        with self.state.lock:
            self.state.intervals[model] = (started, ended)
        return CortexResponse(
            text='{"bucket":"top","justification":"correct"}',
            request_id=f"request-{model}",
            usage={"input_tokens": 1, "output_tokens": 1},
            latency_ms=(ended - started) * 1000,
        )

    def close(self) -> None:
        with self.state.lock:
            self.state.closed_client_count += 1


def test_committee_calls_overlap_and_outputs_keep_model_order() -> None:
    state = ConcurrentState()
    _reward, details, run = evaluate(
        report="## Q1\nAnswer.\n",
        rubric=RUBRIC[:1],
        models=MODELS,
        client_factory=state.factory,
        task="synthetic",
        hashes={},
    )
    assert state.client_count == 3
    assert state.closed_client_count == 3
    assert len(state.intervals) == 3
    assert max(start for start, _end in state.intervals.values()) < min(
        end for _start, end in state.intervals.values()
    )
    assert [vote["model"] for vote in details["questions"][0]["judges"]] == list(MODELS)
    assert [call["model"] for call in run["calls"]] == list(MODELS)
    assert run["committee_concurrency"]["judges_per_question_in_flight"] == 3


class RetryClient:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, model: str, _prompt: str) -> CortexResponse:
        self.calls += 1
        text = (
            "invalid"
            if self.calls == 1
            else '{"bucket":"top","justification":"valid retry"}'
        )
        return CortexResponse(
            text=text,
            request_id=f"{model}-{self.calls}",
            usage={},
            latency_ms=1,
        )


def test_invalid_outputs_retry_once_and_retain_every_attempt() -> None:
    clients: list[RetryClient] = []

    def factory() -> RetryClient:
        client = RetryClient()
        clients.append(client)
        return client

    _reward, details, run = evaluate(
        report="## Q1\nAnswer.\n",
        rubric=RUBRIC[:1],
        models=MODELS,
        client_factory=factory,
        task="synthetic",
        hashes={},
        sleep=lambda _seconds: None,
    )
    assert [client.calls for client in clients] == [2, 2, 2]
    assert len(run["calls"]) == 6
    assert [call["success"] for call in run["calls"]] == [
        False,
        True,
        False,
        True,
        False,
        True,
    ]
    assert [call["retry_delay_seconds"] for call in run["calls"]] == [
        1.0,
        None,
        1.0,
        None,
        1.0,
        None,
    ]
    assert all(vote["attempt_count"] == 2 for vote in details["questions"][0]["judges"])


def test_client_initialization_failure_is_audited_and_fails_closed() -> None:
    def broken_factory() -> FakeClient:
        raise RuntimeError("client setup failed")

    with pytest.raises(EvaluationError, match="incomplete committee") as captured:
        evaluate(
            report="## Q1\nAnswer.\n",
            rubric=RUBRIC[:1],
            models=MODELS,
            client_factory=broken_factory,
            task="synthetic",
            hashes={},
        )
    calls = captured.value.audit["calls"]
    assert len(calls) == 3
    assert all(call["phase"] == "client_initialization" for call in calls)
    assert all(call["request_sent"] is False for call in calls)


@pytest.mark.parametrize("transport_first", [True, False])
def test_mixed_failure_types_share_the_total_attempt_budget(transport_first) -> None:
    class MixedClient:
        calls = 0

        def complete(self, model: str, _prompt: str) -> CortexResponse:
            self.calls += 1
            if (self.calls == 1) == transport_first:
                raise CortexError(
                    "temporary failure", error_class="http_error", retryable=True
                )
            return CortexResponse("invalid", None, {}, 1)

    clients = []

    def factory():
        client = MixedClient()
        clients.append(client)
        return client

    with pytest.raises(EvaluationError, match="incomplete committee") as captured:
        evaluate(
            report="## Q1\nAnswer.",
            rubric=RUBRIC[:1],
            models=MODELS,
            client_factory=factory,
            task="synthetic",
            hashes={},
            sleep=lambda _: None,
        )
    assert [c.calls for c in clients] == [2, 2, 2]
    assert len(captured.value.audit["calls"]) == 6
    assert all(
        not c["retry_scheduled"]
        for c in captured.value.audit["calls"]
        if c["attempt"] == 2
    )


class FailingClient:
    def complete(self, _model: str, _prompt: str) -> CortexResponse:
        raise RuntimeError("non-retryable failure")


def test_score_failure_writes_audit_but_no_partial_scores(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    task = root / "tasks/ecommerce"
    (task / "tests").mkdir(parents=True)
    (root / "data").mkdir()
    (root / "src/avalanche_bench").mkdir(parents=True)
    (task / "instruction.md").write_text("instruction\n")
    (root / "src/avalanche_bench/evaluation.py").write_text("source\n")
    (root / "src/avalanche_bench/extraction.py").write_text("extractor\n")
    (root / "src/avalanche_bench/cortex.py").write_text("transport\n")
    (root / "data/manifest.json").write_text(
        json.dumps({"archives": {"ecommerce": {"sha256": "a" * 64}}})
    )
    with (task / "tests/rubric.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=RUBRIC[0])
        writer.writeheader()
        writer.writerow(RUBRIC[0])
    report = tmp_path / "report.md"
    report.write_text("## Q1\nAnswer.\n")
    reward, details, run = [
        tmp_path / name for name in ("reward.json", "details.json", "run.json")
    ]
    failure = tmp_path / "failure.json"
    with pytest.raises(EvaluationError, match="incomplete committee"):
        score_to_paths(
            task="ecommerce",
            report_path=report,
            reward_path=reward,
            details_path=details,
            run_path=run,
            failure_path=failure,
            root=root,
            client=FailingClient(),
        )
    assert not reward.exists()
    assert not details.exists()
    assert not run.exists()
    payload = json.loads(failure.read_text())
    assert payload["status"] == "failed_closed"
    assert len(payload["audit"]["calls"]) == 3


def test_long_whole_report_fallback_is_not_silently_truncated() -> None:
    report = "Evidence without a marker.\n" + "x" * 120_000
    client = FakeClient(['{"bucket":"zero","justification":"absent"}'] * 3)
    _reward, details, run = evaluate(
        report=report,
        rubric=RUBRIC[:1],
        models=MODELS,
        client=client,
        task="synthetic",
        hashes={},
    )
    assert details["questions"][0]["prompt_scope"] == "whole_report"
    assert run["prompts"][0]["prompt_character_length"] > len(report)
    assert all("x" * 1_000 in prompt for _model, prompt in client.calls)
