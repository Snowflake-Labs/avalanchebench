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

import json
import os
import subprocess
import sys
import threading
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TASKS = ("ecommerce", "customer-support", "travel")


class JudgeHandler(BaseHTTPRequestHandler):
    calls = 0
    invalid = False

    def do_POST(self) -> None:  # noqa: N802
        type(self).calls += 1
        length = int(self.headers["Content-Length"])
        request = json.loads(self.rfile.read(length))
        assert request["temperature"] == 0
        assert request["stream"] is False
        content = (
            "invalid"
            if type(self).invalid
            else '{"bucket":"top","justification":"fixture vote"}'
        )
        response = json.dumps(
            {
                "id": f"request-{type(self).calls}",
                "choices": [{"message": {"content": content}}],
                "usage": {"input_tokens": 10, "output_tokens": 2},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, _format: str, *_args) -> None:
        return


def run_verifier(
    tmp_path: Path, *, invalid: bool, report_link: bool = False
) -> subprocess.CompletedProcess:
    JudgeHandler.calls = 0
    JudgeHandler.invalid = invalid
    server = ThreadingHTTPServer(("127.0.0.1", 0), JudgeHandler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    task = Path(__file__).parents[1] / "tasks/ecommerce"
    report = tmp_path / "report.md"
    if report_link:
        private = tmp_path / "private.txt"
        private.write_text("fixture verifier-only content")
        report.symlink_to(private)
    else:
        report.write_text(
            "\n".join(f"## Q{index}\nFixture answer." for index in range(1, 16))
        )
    env = {
        **os.environ,
        "PYTHONPATH": str(task / "tests"),
        "CORTEX_PAT": "test-pat",
        "CORTEX_REST_ENDPOINT": f"http://127.0.0.1:{server.server_port}",
        "AVBENCH_INCLUDE_PRIVATE_PROMPTS": "1",
        "AVBENCH_JUDGE_CONTEXT_MODE": "whole_report",
        "CORTEX_SNOWFLAKE_CONNECTION_NAME": "",
    }
    outputs = [tmp_path / name for name in ("reward.json", "details.json", "run.json")]
    for output in outputs:
        output.write_text("stale")
    try:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "avalanche_bench.task_verifier",
                "--task",
                "ecommerce",
                "--report",
                str(report),
                "--rubric",
                str(task / "tests/rubric.csv"),
                "--release",
                str(task / "tests/release.json"),
                "--reward-output",
                str(outputs[0]),
                "--details-output",
                str(outputs[1]),
                "--run-output",
                str(outputs[2]),
                "--failure-output",
                str(tmp_path / "failure.json"),
                "--report-artifact",
                str(tmp_path / "report-artifact.md"),
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        server.shutdown()
        thread.join()


def test_self_contained_task_verifier_succeeds_atomically(tmp_path: Path) -> None:
    result = run_verifier(tmp_path, invalid=False)
    assert result.returncode == 0, result.stderr
    assert JudgeHandler.calls == 45
    reward = json.loads((tmp_path / "reward.json").read_text())
    run = json.loads((tmp_path / "run.json").read_text())
    release = json.loads(
        (
            Path(__file__).resolve().parents[1] / "tasks/ecommerce/tests/release.json"
        ).read_text()
    )
    assert reward["reward"] == 1
    if "evaluator_source_tree" in release:
        assert run["evaluator_source_tree"] == release["evaluator_source_tree"]
        assert run["evaluator_source_commit"] is None
    else:
        assert run["evaluator_source_commit"] == release["evaluator_source_commit"]
    assert run["hashes"]["cortex_transport_sha256"] == release["cortex_sha256"]
    assert run["hashes"]["extractor_sha256"] == release["extractor_sha256"]
    assert run["catc"] == "not_calculated_for_judge_calls"
    assert run["judge_context_mode"] == "whole_report"
    assert all("prompt" in prompt for prompt in run["prompts"])
    assert (tmp_path / "report-artifact.md").read_bytes() == (
        tmp_path / "report.md"
    ).read_bytes()


def test_self_contained_task_verifier_failure_removes_stale_outputs(
    tmp_path: Path,
) -> None:
    result = run_verifier(tmp_path, invalid=True)
    assert result.returncode != 0
    assert JudgeHandler.calls == 6
    assert not (tmp_path / "reward.json").exists()
    assert not (tmp_path / "details.json").exists()
    assert not (tmp_path / "run.json").exists()
    failure = json.loads((tmp_path / "failure.json").read_text())
    assert failure["status"] == "failed_closed"
    assert len(failure["audit"]["calls"]) == 6
    assert (tmp_path / "report-artifact.md").is_file()


def test_self_contained_verifier_rejects_link_before_export_or_judging(
    tmp_path: Path,
) -> None:
    result = run_verifier(tmp_path, invalid=False, report_link=True)
    assert result.returncode != 0
    assert "without symbolic links" in result.stderr
    assert JudgeHandler.calls == 0
    assert not (tmp_path / "report-artifact.md").exists()
    assert not (tmp_path / "reward.json").exists()
    assert not (tmp_path / "details.json").exists()


def test_readme_score_command_writes_complete_outputs(tmp_path: Path) -> None:
    JudgeHandler.calls = 0
    JudgeHandler.invalid = False
    server = ThreadingHTTPServer(("127.0.0.1", 0), JudgeHandler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    report = tmp_path / "report.md"
    report.write_text(
        "\n".join(f"## Q{index}\nFixture answer." for index in range(1, 16))
    )
    output = tmp_path / "score"
    env = {
        **os.environ,
        "CORTEX_PAT": "test-pat",
        "CORTEX_REST_ENDPOINT": f"http://127.0.0.1:{server.server_port}",
        "CORTEX_SNOWFLAKE_CONNECTION_NAME": "",
    }
    try:
        result = subprocess.run(
            [
                str(Path(sys.executable).with_name("avbench")),
                "score",
                "--task",
                "ecommerce",
                "--report",
                str(report),
                "--output-dir",
                str(output),
            ],
            cwd=Path(__file__).parents[1],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        server.shutdown()
        thread.join()
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(output)
    assert JudgeHandler.calls == 45
    assert json.loads((output / "reward.json").read_text())["reward"] == 1
    assert len(json.loads((output / "evaluation.json").read_text())["questions"]) == 15
    assert len(json.loads((output / "run.json").read_text())["calls"]) == 45


def test_task_manifests_forward_cortex_token_type() -> None:
    root = Path(__file__).parents[1]
    expected = "${CORTEX_TOKEN_TYPE:-PROGRAMMATIC_ACCESS_TOKEN}"
    for task in TASKS:
        payload = tomllib.loads((root / "tasks" / task / "task.toml").read_text())
        assert payload["verifier"]["env"]["CORTEX_TOKEN_TYPE"] == expected
        assert payload["verifier"]["env"]["CORTEX_JUDGE_OUTPUT_LIMIT"] == (
            "${CORTEX_JUDGE_OUTPUT_LIMIT:-4096}"
        )
        assert "CORTEX_JUDGE_MAX_TOKENS" not in payload["verifier"]["env"]
        assert payload["verifier"]["env"]["CORTEX_REST_CROSS_REGION"] == (
            "${CORTEX_REST_CROSS_REGION:-ANY_REGION}"
        )
        assert "AVBENCH_JUDGE_CONTEXT_MODE" in payload["verifier"]["env"]
        assert "AVBENCH_INCLUDE_PRIVATE_PROMPTS" in payload["verifier"]["env"]


def test_task_scripts_delegate_report_export_to_the_verifier() -> None:
    root = Path(__file__).parents[1]
    for task in TASKS:
        script = (root / "tasks" / task / "tests/test.sh").read_text()
        assert "cp /app/report.md" not in script
        assert "--report-artifact /logs/artifacts/report.md" in script
