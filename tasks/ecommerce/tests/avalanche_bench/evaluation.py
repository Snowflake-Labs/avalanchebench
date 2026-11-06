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

"""Published AvalancheBench rubric parser and three-judge aggregation."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import stat
import statistics
import tempfile
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from . import __version__
from .cortex import (
    DEFAULT_MAX_TOKENS,
    ENDPOINT_PATH,
    JUDGE_OUTPUT_LIMIT_ENV,
    CortexError,
    CortexResponse,
)
from .extraction import extract_questions
from .extraction import report_section as robust_report_section

AGGREGATION = "median_bucket_score"
CONTEXT_MODES = {
    "robust_section_only",
    "whole_report",
    "section_first_fallback",
}
DEFAULT_CONTEXT_MODE = "section_first_fallback"
DEFAULT_TRANSPORT_ATTEMPTS = 2
DEFAULT_INVALID_OUTPUT_ATTEMPTS = 2
RUBRIC_FIELDS = {
    "category",
    "max_points",
    "question",
    "scoring_criteria",
    "subcategory",
    "target_answer",
}
BUCKET_ALIASES = {
    "top": "top",
    "perfect": "top",
    "middle": "middle",
    "half": "middle",
    "low": "low",
    "partial": "low",
    "zero": "zero",
}


class EvaluationError(RuntimeError):
    """Evaluation could not produce a complete committee result."""

    def __init__(self, message: str, *, audit: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.audit = audit or {}


class JudgeOutputError(EvaluationError):
    """A judge response was syntactically invalid and may be retried."""


class JudgeClient(Protocol):
    def complete(self, model: str, prompt: str) -> CortexResponse: ...


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


MAX_REPORT_BYTES = 16 * 1024 * 1024


def read_report(path: Path) -> bytes:
    """Snapshot an untrusted report without following links or special files."""

    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise EvaluationError("secure report loading requires a Unix filesystem")
    path = Path(os.path.abspath(path))
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        directory = os.open(path.anchor, directory_flags)
        try:
            for part in path.parts[1:-1]:
                child = os.open(part, directory_flags, dir_fd=directory)
                os.close(directory)
                directory = child
            descriptor = os.open(
                path.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=directory,
            )
        finally:
            os.close(directory)
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise EvaluationError("report must be a regular file without links")
            if before.st_size > MAX_REPORT_BYTES:
                raise EvaluationError("report exceeds the 16 MiB size limit")
            content = handle.read(MAX_REPORT_BYTES + 1)
            after = os.fstat(handle.fileno())
        if len(content) > MAX_REPORT_BYTES:
            raise EvaluationError("report exceeds the 16 MiB size limit")
        if (
            len(content) != before.st_size
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or after.st_nlink != 1
        ):
            raise EvaluationError("report changed while it was being read")
        return content
    except OSError as exc:
        raise EvaluationError(
            "report must be a readable regular file without symbolic links"
        ) from exc


def report_section(report: str, question_number: int) -> str:
    return robust_report_section(report, question_number)


def score_bucket_map(criteria: str, max_points: float) -> dict[str, float]:
    buckets: dict[str, float] = {}
    pattern = re.compile(
        r"(?i)\b(top|perfect|middle|half|low|partial|zero)\s*"
        r"\((\d+(?:\.\d+)?)\s*pt[s]?\)"
    )
    for label, raw_score in pattern.findall(criteria):
        buckets[BUCKET_ALIASES[label.lower()]] = float(raw_score)
    buckets.setdefault("top", max_points)
    buckets.setdefault("zero", 0.0)
    if not buckets or any(
        score < 0 or score > max_points for score in buckets.values()
    ):
        raise EvaluationError("rubric contains an invalid score bucket")
    return buckets


def evaluation_prompt(
    row: dict[str, str], content: str, buckets: dict[str, float], *, prompt_scope: str
) -> str:
    if prompt_scope not in {"section", "whole_report"}:
        raise EvaluationError(f"unsupported prompt scope {prompt_scope!r}")
    bucket_text = ", ".join(f"{key}={value:g}" for key, value in buckets.items())
    introduction = (
        "You are one member of an independent evaluation committee assessing a "
        "data-analysis report."
    )
    source_dump_rule = (
        "- Raw source dumps, quoted data, or query output do not count unless the "
        "report states a conclusion."
    )
    if prompt_scope == "section":
        content_label = "REPORT SECTION THAT ANSWERS THIS QUESTION"
        scope_rule = "- Score only claims made in this section."
    else:
        content_label = "FULL REPORT"
        scope_rule = (
            "- Score only the report's claims that answer this rubric question; "
            "do not reward unrelated answers."
        )
    return f"""{introduction}
Apply the rubric exactly and return only the requested JSON object.

RUBRIC QUESTION
Category: {row.get("category", "")}
Subcategory: {row.get("subcategory", "")}
Question: {row.get("question", "")}
Target answer: {row.get("target_answer", "")}
Scoring criteria: {row.get("scoring_criteria", "")}
Maximum points: {row.get("max_points", "5")}

{content_label}
{content}

SCORING RULES
{scope_rule}
- Apply the scoring criteria literally; do not reward plausible but incorrect claims.
- Correctly reasoned negative findings can earn partial credit when the method is sound.
{source_dump_rule}
- Length is not merit. Unsupported confidence must not raise the score.
- The justification must agree with the selected bucket.
- Select exactly one allowed bucket: {bucket_text}.

Return valid JSON only. The bucket must be one of: {"|".join(buckets)}
{{"bucket":"one allowed bucket","justification":"brief explanation"}}
"""


def _extract_json_object(text: str) -> dict[str, Any]:
    stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    try:
        payload = json.loads(stripped)
        if isinstance(payload, dict):
            return payload
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for index, character in enumerate(stripped):
        if character != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(stripped[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise JudgeOutputError("judge output does not contain a valid JSON object")


def extract_judge_vote(text: str, buckets: dict[str, float]) -> tuple[str, str]:
    payload = _extract_json_object(text)
    if "bucket" not in payload:
        raise JudgeOutputError("judge output has no bucket")
    bucket = payload["bucket"]
    justification = payload.get("justification")
    canonical = (
        BUCKET_ALIASES.get(bucket.strip().lower()) if isinstance(bucket, str) else None
    )
    if canonical not in buckets:
        raise JudgeOutputError(f"judge returned unsupported bucket {bucket!r}")
    if not isinstance(justification, str) or not justification.strip():
        raise JudgeOutputError("judge returned no justification")
    return canonical, justification.strip()


def load_rubric(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        while True:
            position = handle.tell()
            if not handle.readline().startswith("#"):
                handle.seek(position)
                break
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        if len(fieldnames) != len(set(fieldnames)):
            raise EvaluationError("rubric contains duplicate columns")
        missing = sorted(RUBRIC_FIELDS - set(fieldnames))
        if missing:
            raise EvaluationError(
                "rubric is missing required columns: " + ", ".join(missing)
            )
        rows = list(reader)
    if not rows:
        raise EvaluationError("rubric is empty")
    for index, row in enumerate(rows, start=1):
        if any(not row.get(field, "").strip() for field in RUBRIC_FIELDS):
            raise EvaluationError(f"rubric row {index} contains an empty field")
        try:
            maximum = float(row["max_points"])
        except ValueError as exc:
            raise EvaluationError(f"rubric row {index} max_points is invalid") from exc
        if maximum <= 0:
            raise EvaluationError(f"rubric row {index} max_points must be positive")
        score_bucket_map(row["scoring_criteria"], maximum)
    return rows


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _committee(votes: list[dict[str, Any]]) -> dict[str, Any]:
    if len(votes) != 3:
        raise EvaluationError("committee requires exactly three successful votes")
    median_score = float(statistics.median(vote["score"] for vote in votes))
    median_vote = next(vote for vote in votes if vote["score"] == median_score)
    counts = Counter(vote["bucket"] for vote in votes)
    return {
        "score": median_score,
        "bucket": median_vote["bucket"],
        "aggregation": AGGREGATION,
        "unanimous": len(counts) == 1,
        "vote_counts": dict(sorted(counts.items())),
        "judges": votes,
    }


def _rubric_content_sha256(rubric: list[dict[str, str]]) -> str:
    payload = json.dumps(
        rubric, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return sha256_bytes(payload.encode())


def _judge_one(
    *,
    question_id: int,
    model: str,
    model_order: int,
    prompt: str,
    prompt_sha256: str,
    buckets: dict[str, float],
    client: JudgeClient | None,
    client_factory: Callable[[], JudgeClient] | None,
    transport_attempts: int,
    invalid_output_attempts: int,
    retry_backoff_seconds: float,
    sleep: Callable[[float], None],
    now: Callable[[], datetime],
) -> dict[str, Any]:
    initialization_started = now()
    try:
        judge = client_factory() if client_factory is not None else client
    except Exception as exc:
        initialization_ended = now()
        return {
            "success": False,
            "model": model,
            "model_order": model_order,
            "attempts": [
                {
                    "attempt": 1,
                    "phase": "client_initialization",
                    "started_at": initialization_started.isoformat(),
                    "ended_at": initialization_ended.isoformat(),
                    "model": model,
                    "requested_model": model,
                    "resolved_model": None,
                    "request_sent": False,
                    "request_id": None,
                    "latency_ms": None,
                    "usage": {},
                    "success": False,
                    "error_class": getattr(
                        exc, "error_class", "client_initialization_error"
                    ),
                    "error_message": str(exc),
                    "retryable": False,
                    "retry_scheduled": False,
                    "retry_delay_seconds": None,
                    "prompt_sha256": prompt_sha256,
                }
            ],
            "error": str(exc),
        }
    if judge is None:
        raise EvaluationError("a judge client or client factory is required")
    endpoint_path = getattr(judge, "endpoint_path", ENDPOINT_PATH)
    endpoint_host = urlsplit(getattr(judge, "endpoint", "") or "").hostname
    token_type = getattr(judge, "token_type", None)
    cross_region = getattr(
        judge, "cross_region", os.environ.get("CORTEX_REST_CROSS_REGION", "ANY_REGION")
    )

    def finish(payload: dict[str, Any]) -> dict[str, Any]:
        if client_factory is not None and callable(getattr(judge, "close", None)):
            try:
                judge.close()  # type: ignore[attr-defined]
            except Exception as exc:  # closing must not alter a completed vote
                payload["client_close_error"] = f"{type(exc).__name__}: {exc}"
        return payload

    attempts: list[dict[str, Any]] = []
    attempt_number = 0
    while True:
        attempt_number += 1
        started_at = now()
        try:
            response = judge.complete(model, prompt)
        except Exception as exc:
            ended_at = now()
            is_cortex_error = isinstance(exc, CortexError)
            error_class = (
                getattr(exc, "error_class", None)
                if is_cortex_error
                else "client_exception"
            ) or "cortex_error"
            retryable = bool(getattr(exc, "retryable", False))
            invalid_response = error_class in {
                "empty_response",
                "invalid_json_response",
                "invalid_response",
            }
            if invalid_response:
                retry_allowed = retryable and attempt_number < invalid_output_attempts
            else:
                retry_allowed = retryable and attempt_number < transport_attempts
            attempts.append(
                {
                    "attempt": attempt_number,
                    "started_at": started_at.isoformat(),
                    "ended_at": ended_at.isoformat(),
                    "model": model,
                    "requested_model": model,
                    "resolved_model": None,
                    "request_sent": True,
                    "endpoint_path": endpoint_path,
                    "endpoint_host": endpoint_host,
                    "token_type": token_type,
                    "cross_region": cross_region,
                    "request_id": getattr(exc, "request_id", None),
                    "latency_ms": getattr(exc, "latency_ms", None),
                    "usage": getattr(exc, "usage", {}),
                    "success": False,
                    "error_class": error_class,
                    "error_message": str(exc),
                    "retryable": retryable,
                    "retry_scheduled": retry_allowed,
                    "retry_delay_seconds": (
                        retry_backoff_seconds if retry_allowed else None
                    ),
                    "prompt_sha256": prompt_sha256,
                }
            )
            if retry_allowed:
                sleep(retry_backoff_seconds)
                continue
            return finish(
                {
                    "success": False,
                    "model": model,
                    "model_order": model_order,
                    "attempts": attempts,
                    "error": str(exc),
                }
            )

        ended_at = now()
        base_attempt = {
            "attempt": attempt_number,
            "started_at": started_at.isoformat(),
            "ended_at": ended_at.isoformat(),
            "model": model,
            "requested_model": model,
            "resolved_model": response.resolved_model,
            "request_sent": True,
            "endpoint_path": endpoint_path,
            "endpoint_host": endpoint_host,
            "token_type": token_type,
            "cross_region": cross_region,
            "request_id": response.request_id,
            "latency_ms": response.latency_ms,
            "usage": response.usage,
            "prompt_sha256": prompt_sha256,
        }
        try:
            bucket, justification = extract_judge_vote(response.text, buckets)
        except JudgeOutputError as exc:
            retry_allowed = attempt_number < invalid_output_attempts
            attempts.append(
                {
                    **base_attempt,
                    "success": False,
                    "error_class": "invalid_judge_output",
                    "error_message": str(exc),
                    "retryable": True,
                    "retry_scheduled": retry_allowed,
                    "retry_delay_seconds": (
                        retry_backoff_seconds if retry_allowed else None
                    ),
                }
            )
            if retry_allowed:
                sleep(retry_backoff_seconds)
                continue
            return finish(
                {
                    "success": False,
                    "model": model,
                    "model_order": model_order,
                    "attempts": attempts,
                    "error": str(exc),
                }
            )
        attempts.append(
            {
                **base_attempt,
                "success": True,
                "error_class": None,
                "error_message": None,
                "retryable": False,
                "retry_scheduled": False,
                "retry_delay_seconds": None,
            }
        )
        return finish(
            {
                "success": True,
                "model": model,
                "model_order": model_order,
                "attempts": attempts,
                "vote": {
                    "model": model,
                    "requested_model": model,
                    "resolved_model": response.resolved_model,
                    "bucket": bucket,
                    "score": buckets[bucket],
                    "justification": justification,
                    "request_id": response.request_id,
                    "usage": response.usage,
                    "latency_ms": response.latency_ms,
                    "attempt_count": attempt_number,
                    "prompt_sha256": prompt_sha256,
                },
            }
        )


def evaluate(
    *,
    report: str,
    rubric: list[dict[str, str]],
    models: tuple[str, str, str],
    client: JudgeClient | None = None,
    client_factory: Callable[[], JudgeClient] | None = None,
    task: str,
    hashes: dict[str, str],
    source_commit: str | None = None,
    source_tree: str | None = None,
    context_mode: str | None = None,
    transport_attempts: int = DEFAULT_TRANSPORT_ATTEMPTS,
    invalid_output_attempts: int = DEFAULT_INVALID_OUTPUT_ATTEMPTS,
    retry_backoff_seconds: float = 1.0,
    include_private_prompts: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Evaluate a report with auditable extraction and a concurrent committee."""

    if len(models) != 3 or len(set(models)) != 3:
        raise EvaluationError("exactly three distinct judge models are required")
    if (client is None) == (client_factory is None):
        raise EvaluationError("provide exactly one of client or client_factory")
    mode = context_mode or os.environ.get(
        "AVBENCH_JUDGE_CONTEXT_MODE", DEFAULT_CONTEXT_MODE
    )
    if mode not in CONTEXT_MODES:
        raise EvaluationError(f"unsupported judge context mode {mode!r}")
    if (
        transport_attempts < 1
        or invalid_output_attempts < 1
        or retry_backoff_seconds < 0
    ):
        raise EvaluationError("judge attempt limits must be positive")

    started = now()
    questions: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    prompts: list[dict[str, Any]] = []
    total = 0.0
    maximum = 0.0
    category_earned: dict[str, float] = defaultdict(float)
    category_maximum: dict[str, float] = defaultdict(float)
    unanimous_count = 0
    completed = 0
    report_hash = hashes.get("report_sha256") or sha256_bytes(report.encode())
    rubric_hash = hashes.get("rubric_sha256") or _rubric_content_sha256(rubric)
    extractions = extract_questions(
        report,
        rubric,
        report_sha256=report_hash,
        rubric_sha256=rubric_hash,
    )

    decisions: list[tuple[str | None, str, dict[str, Any]]] = []
    for extraction in extractions:
        if extraction.fallback_reason == "empty_report":
            scope = "none_empty_report"
            content = None
        elif mode == "whole_report":
            scope = "whole_report"
            content = report
        elif extraction.selected is not None:
            scope = "section"
            content = extraction.text
        elif mode == "section_first_fallback":
            scope = "whole_report"
            content = report
        else:
            provenance = extraction.provenance("unresolved")
            raise EvaluationError(
                f"Q{extraction.question_id} has no unique robust section under "
                "robust_section_only",
                audit={"extraction": provenance, "context_mode": mode},
            )
        provenance = extraction.provenance(scope)
        if mode == "whole_report" and provenance["fallback_reason"] is None:
            provenance["fallback_reason"] = "context_policy_whole_report"
        decisions.append((content, scope, provenance))

    for question_number, row in enumerate(rubric, start=1):
        max_points = float(row["max_points"])
        maximum += max_points
        category = row["category"]
        category_maximum[category] += max_points
        content, prompt_scope, provenance = decisions[question_number - 1]
        if content is None:
            questions.append(
                {
                    "question_id": question_number,
                    "category": category,
                    "subcategory": row["subcategory"],
                    "question": row["question"],
                    "max_points": max_points,
                    "score": 0.0,
                    "bucket": "zero",
                    "justification": "The report was empty or whitespace-only.",
                    "evaluation_method": "empty_report",
                    "extraction": provenance,
                }
            )
            continue

        buckets = score_bucket_map(row["scoring_criteria"], max_points)
        prompt = evaluation_prompt(
            row,
            content,
            buckets,
            prompt_scope=prompt_scope,
        )
        prompt_hash = sha256_bytes(prompt.encode())
        prompt_record: dict[str, Any] = {
            "question_id": question_number,
            "prompt_scope": prompt_scope,
            "prompt_sha256": prompt_hash,
            "prompt_character_length": len(prompt),
            "prompt_byte_length": len(prompt.encode("utf-8")),
        }
        if include_private_prompts:
            prompt_record["prompt"] = prompt
        prompts.append(prompt_record)

        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [
                executor.submit(
                    _judge_one,
                    question_id=question_number,
                    model=model,
                    model_order=model_order,
                    prompt=prompt,
                    prompt_sha256=prompt_hash,
                    buckets=buckets,
                    client=client,
                    client_factory=client_factory,
                    transport_attempts=transport_attempts,
                    invalid_output_attempts=invalid_output_attempts,
                    retry_backoff_seconds=retry_backoff_seconds,
                    sleep=sleep,
                    now=now,
                )
                for model_order, model in enumerate(models)
            ]
            outcomes = [future.result() for future in futures]

        question_calls: list[dict[str, Any]] = []
        for outcome in outcomes:
            for attempt in outcome["attempts"]:
                question_calls.append(
                    {
                        "question_id": question_number,
                        "model_order": outcome["model_order"],
                        **attempt,
                    }
                )
        question_calls.sort(key=lambda item: (item["model_order"], item["attempt"]))
        for attempt in question_calls:
            attempt["call_index"] = len(calls) + 1
            calls.append(attempt)
        failures = [outcome for outcome in outcomes if not outcome["success"]]
        if failures:
            raise EvaluationError(
                "incomplete committee for Q"
                f"{question_number}: "
                + ", ".join(
                    f"{failure['model']}: {failure['error']}" for failure in failures
                ),
                audit={
                    "question_id": question_number,
                    "context_mode": mode,
                    "prompt_sha256": prompt_hash,
                    "extraction": provenance,
                    "calls": calls,
                },
            )
        votes = [outcome["vote"] for outcome in outcomes]
        committee = _committee(votes)
        score = committee["score"]
        total += score
        category_earned[category] += score
        unanimous_count += int(committee["unanimous"])
        completed += 1
        questions.append(
            {
                "question_id": question_number,
                "category": category,
                "subcategory": row["subcategory"],
                "question": row["question"],
                "max_points": max_points,
                "evaluation_method": "snowflake_cortex_rest_committee",
                "prompt_scope": prompt_scope,
                "prompt_sha256": prompt_hash,
                "extraction": provenance,
                **committee,
            }
        )

    normalized = total / maximum if maximum else 0.0
    agreement = unanimous_count / completed if completed else 0.0
    reward: dict[str, float] = {
        "reward": normalized,
        "avalanche_score": normalized,
        "total_points": total,
        "max_points": maximum,
        "completion": completed / len(rubric),
        "committee_unanimous_fraction": agreement,
    }
    for item in questions:
        reward[f"q{item['question_id']:02d}"] = item["score"] / item["max_points"]
    for category, category_max in category_maximum.items():
        reward[f"category_{_slug(category)}"] = category_earned[category] / category_max

    ended = now()
    details = {
        "task": task,
        "judge_models": list(models),
        "judge_context_mode": mode,
        "committee_aggregation": AGGREGATION,
        "total_points": total,
        "max_points": maximum,
        "normalized_score": normalized,
        "committee_unanimous_fraction": agreement,
        "questions": questions,
    }
    endpoint_paths = sorted(
        {call["endpoint_path"] for call in calls if call.get("endpoint_path")}
    )
    endpoint_hosts = sorted(
        {call["endpoint_host"] for call in calls if call.get("endpoint_host")}
    )
    token_types = sorted(
        {call["token_type"] for call in calls if call.get("token_type")}
    )
    run = {
        "evaluator_version": __version__,
        "evaluator_source_commit": source_commit
        or os.environ.get("AVBENCH_SOURCE_COMMIT")
        or (None if source_tree else "working-tree"),
        "evaluator_source_tree": source_tree,
        "task": task,
        "hashes": hashes,
        "endpoint_path": endpoint_paths[0] if len(endpoint_paths) == 1 else None,
        "endpoint_paths": endpoint_paths,
        "endpoint_host": endpoint_hosts[0] if len(endpoint_hosts) == 1 else None,
        "endpoint_hosts": endpoint_hosts,
        "endpoint_is_default": not bool(os.environ.get("CORTEX_REST_ENDPOINT")),
        "token_type": token_types[0] if len(token_types) == 1 else None,
        "cross_region": (
            sorted({call["cross_region"] for call in calls}) if calls else []
        ),
        "models": list(models),
        "temperature": 0,
        "max_tokens": int(
            os.environ.get(JUDGE_OUTPUT_LIMIT_ENV, str(DEFAULT_MAX_TOKENS))
        ),
        "aggregation": AGGREGATION,
        "judge_context_mode": mode,
        "committee_concurrency": {
            "questions_in_flight": 1,
            "judges_per_question_in_flight": 3,
            "client_mode": (
                "independent_client_per_judge"
                if client_factory is not None
                else "caller_supplied_shared_client"
            ),
        },
        "retry_policy": {
            "max_total_attempts": max(transport_attempts, invalid_output_attempts),
            "transport_max_attempts": transport_attempts,
            "invalid_output_max_attempts": invalid_output_attempts,
            "retry_backoff_seconds": retry_backoff_seconds,
            "low_score_retry": False,
        },
        "started_at": started.isoformat(),
        "ended_at": ended.isoformat(),
        "prompts": prompts,
        "extractions": [question["extraction"] for question in questions],
        "calls": calls,
        "catc": "not_calculated_for_judge_calls",
    }
    return reward, details, run


def prompt_template_sha256() -> str:
    placeholder = {
        "category": "{category}",
        "subcategory": "{subcategory}",
        "question": "{question}",
        "target_answer": "{target_answer}",
        "scoring_criteria": "{scoring_criteria}",
        "max_points": "{max_points}",
    }
    templates = {
        scope: evaluation_prompt(
            placeholder,
            "{report_content}",
            {"top": 5, "zero": 0},
            prompt_scope=scope,
        )
        for scope in ("section", "whole_report")
    }
    return sha256_bytes(
        json.dumps(templates, sort_keys=True, separators=(",", ":")).encode()
    )


def _stage_bytes(final: Path, content: bytes, *, mode: int = 0o600) -> Path:
    final.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=f".{final.name}.", suffix=".tmp", dir=final.parent
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            os.fchmod(handle.fileno(), mode)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def write_bytes_atomically(final: Path, content: bytes) -> None:
    """Replace one artifact using an exclusively created private staging file."""

    temporary = _stage_bytes(final, content)
    try:
        os.replace(temporary, final)
    finally:
        temporary.unlink(missing_ok=True)


def write_outputs_atomically(
    outputs: Iterable[tuple[Path, dict[str, Any]]], *, mode: int = 0o600
) -> None:
    """Publish JSON files only after every complete payload is serialized."""

    prepared: list[tuple[Path, Path]] = []
    output_list = list(outputs)
    for final, _ in output_list:
        final.unlink(missing_ok=True)
    try:
        for final, payload in output_list:
            content = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
            temporary = _stage_bytes(final, content, mode=mode)
            prepared.append((temporary, final))
        for temporary, final in prepared:
            os.replace(temporary, final)
    except BaseException:
        for temporary, _ in prepared:
            temporary.unlink(missing_ok=True)
        for final, _ in output_list:
            final.unlink(missing_ok=True)
        raise
