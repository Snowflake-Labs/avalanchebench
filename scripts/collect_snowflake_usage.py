#!/usr/bin/env python3
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

"""Collect agent and agent-issued Snowflake AISQL usage for CATC and monetary cost."""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import snowflake.connector

from avalanche_bench.catc import (
    TokenUsage,
    aggregate_catc,
    load_snapshot,
    measure_component,
    normalize_usage,
)
from avalanche_bench.evaluation import write_bytes_atomically
from avalanche_bench.monetary import (
    DEFAULT_PRICES,
    aggregate_monetary_cost,
    load_prices,
    measure_cost_component,
)

ROOT = Path(__file__).resolve().parents[1]
QUERY_ID = re.compile(r"\b01[0-9a-f]{6}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f-]{12,}\b", re.I)
AI_FUNCTION = re.compile(
    r"\b(?:(?:SNOWFLAKE[.])?CORTEX[.]([A-Z_]+)|(AI_[A-Z_]+))\s*\(", re.I
)
AI_COMPLETE_MODEL = re.compile(
    r"\b(?:AI_COMPLETE|(?:SNOWFLAKE[.])?CORTEX[.]COMPLETE)\s*"
    r"\(\s*(?:MODEL\s*=>\s*)?'([^']+)'",
    re.I,
)
SQL_NON_CODE = re.compile(r"\$\$.*?\$\$|'(?:''|[^'])*'|--[^\n]*|/\*.*?\*/", re.S)
NON_BILLABLE_AI = {"AI_COUNT_TOKENS"}
CORTEX_CODE_APP = "cortex_code_cli"
DEFAULT_LEDGER_LAG_SECONDS = 3 * 60 * 60


def ledger_error(label: str, error: Exception) -> str:
    """Describe a failed ledger without copying SQL or connection details."""
    code = getattr(error, "errno", None)
    return f"{label} unavailable ({type(error).__name__}, code {code})"


def potentially_hidden_ai(query_text: str) -> bool:
    code = SQL_NON_CODE.sub("", query_text).strip()
    return bool(re.match(r"(?:CALL\b|EXECUTE\s+(?:IMMEDIATE|TASK)\b)", code, re.I))


def coverage_reasons(
    *,
    window: tuple[datetime, datetime] | None,
    statements: list[dict[str, Any]],
    query_ids: set[str],
    agent_user: str | None,
    collected_at: datetime,
    minimum_ledger_age_seconds: int,
) -> list[str]:
    """Do not infer a complete SQL ledger from missing observations."""
    reasons = []
    if not agent_user:
        reasons.append("agent user identity unverified; supply --agent-user")
    if window is None:
        reasons.append("no Harbor agent window; only trajectory SQL is attributed")
    elif collected_at < window[1] + timedelta(seconds=minimum_ledger_age_seconds):
        reasons.append(
            "agent window is newer than the configured Account Usage lag bound"
        )
    if not statements:
        reasons.append("no assigned session statements; zero AISQL usage is unverified")
    missing = query_ids - {str(row["QUERY_ID"]) for row in statements}
    if missing:
        reasons.append("query history does not cover all trajectory query IDs")
    if any(
        potentially_hidden_ai(str(row.get("QUERY_TEXT") or "")) for row in statements
    ):
        reasons.append(
            "procedure or dynamic SQL may contain unattributed AI operations"
        )
    return reasons


def json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def metric_amount(value: Any, metric: str, unit: str) -> int | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    if not isinstance(value, list):
        return None
    amounts = []
    for item in value:
        key = json_object(item.get("key")) if isinstance(item, dict) else {}
        if (
            str(key.get("metric")).lower() == metric
            and str(key.get("unit")).lower() == unit
        ):
            amounts.append(int(item["value"]))
    return sum(amounts) if amounts else None


def add_usage(totals: dict[tuple[str, bool], list[int]], usage: TokenUsage) -> None:
    values = totals.setdefault((usage.model_name, usage.estimated), [0, 0, 0, 0])
    values[0] += usage.input_tokens
    values[1] += usage.cache_read_tokens
    values[2] += usage.cache_write_tokens
    values[3] += usage.output_tokens


def usages_from_totals(
    totals: dict[tuple[str, bool], list[int]],
) -> list[TokenUsage]:
    return [
        TokenUsage(model, *values, estimated=estimated)
        for (model, estimated), values in sorted(totals.items())
    ]


def load_aisql_estimates(path: Path) -> dict[str, dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("AISQL estimate config must be a JSON object")
    for function, estimate in payload.items():
        if not isinstance(estimate, dict):
            raise ValueError(f"invalid AISQL estimate for {function}")
        model = estimate.get("reference_model")
        fraction = estimate.get("input_fraction")
        page_tokens = (
            estimate.get("input_tokens_per_page"),
            estimate.get("output_tokens_per_page"),
        )
        fraction_estimate = isinstance(fraction, (int, float))
        page_estimate = all(
            isinstance(value, int) and value >= 0 for value in page_tokens
        )
        if not isinstance(model, str) or fraction_estimate == page_estimate:
            raise ValueError(f"invalid AISQL estimate for {function}")
        if fraction_estimate and not 0 <= fraction <= 1:
            raise ValueError(f"input_fraction must be between 0 and 1 for {function}")
    return {str(name).upper(): value for name, value in payload.items()}


def ai_functions(query_text: str) -> tuple[str, ...]:
    names = set()
    for match in AI_FUNCTION.finditer(SQL_NON_CODE.sub("", query_text)):
        name = str(match.group(1) or match.group(2)).upper()
        name = name if name.startswith("AI_") else f"AI_{name}"
        if name not in NON_BILLABLE_AI:
            names.add(name)
    return tuple(sorted(names))


def ai_complete_model(query_text: str) -> str | None:
    match = AI_COMPLETE_MODEL.search(query_text)
    return match.group(1) if match else None


def estimated_usage(
    function: str,
    total_tokens: int,
    estimates: dict[str, dict[str, Any]],
) -> TokenUsage:
    estimate = estimates[function]
    input_tokens = round(total_tokens * float(estimate["input_fraction"]))
    return TokenUsage(
        model_name=str(estimate["reference_model"]),
        input_tokens=input_tokens,
        output_tokens=total_tokens - input_tokens,
        estimated=True,
    )


def estimated_page_usage(
    function: str,
    pages: int,
    estimates: dict[str, dict[str, Any]],
) -> TokenUsage:
    estimate = estimates[function]
    return TokenUsage(
        model_name=str(estimate["reference_model"]),
        input_tokens=pages * int(estimate["input_tokens_per_page"]),
        output_tokens=pages * int(estimate["output_tokens_per_page"]),
        estimated=True,
    )


def harbor_agent_usage(
    result: dict[str, Any],
    agent_log: Path | None = None,
) -> tuple[list[TokenUsage], str, str | None]:
    agent_result = json_object(result.get("agent_result"))
    model = str(
        json_object(result.get("agent_info")).get("model_info", {}).get("name")
        or json_object(result.get("config")).get("agent", {}).get("model_name")
        or "unknown"
    )
    for container in (
        json_object(agent_result.get("metadata")),
        json_object(agent_result.get("rollout_details")),
    ):
        raw = json_object(container.get("usage"))
        if raw:
            return [normalize_usage(model, raw)], "harbor_result_detailed_usage", None

    if agent_log and agent_log.is_file():
        for line in reversed(agent_log.read_text(encoding="utf-8").splitlines()):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            raw = json_object(event.get("usage"))
            if event.get("type") == "result" and raw:
                return [normalize_usage(model, raw)], "cortex_code_result_event", None

    cache_read = agent_result.get("n_cache_read_tokens")
    cache_write = agent_result.get("n_cache_write_tokens")
    if cache_read is not None or cache_write is not None:
        input_total = int(agent_result.get("n_input_tokens") or 0)
        cache_read = int(cache_read or 0)
        cache_write = int(cache_write or 0)
        return (
            [
                TokenUsage(
                    model,
                    input_tokens=max(input_total - cache_read - cache_write, 0),
                    cache_read_tokens=cache_read,
                    cache_write_tokens=cache_write,
                    output_tokens=int(agent_result.get("n_output_tokens") or 0),
                )
            ],
            "harbor_result_detailed_aggregate",
            None,
        )

    input_total = int(agent_result.get("n_input_tokens") or 0)
    cache_total = int(agent_result.get("n_cache_tokens") or 0)
    output = int(agent_result.get("n_output_tokens") or 0)
    if input_total or cache_total or output:
        return (
            [
                TokenUsage(
                    model,
                    input_tokens=max(input_total - cache_total, 0),
                    cache_read_tokens=cache_total,
                    output_tokens=output,
                )
            ],
            "harbor_result_aggregate_fallback",
            (
                "Harbor aggregate cache usage does not distinguish reads from writes; "
                "all cache tokens are counted at the lower read weight"
            ),
        )
    return [], "harbor_result", "Harbor result contains no attributable token usage"


def find_query_ids(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() == "query_id" and isinstance(child, str):
                found.update(QUERY_ID.findall(child))
            else:
                found.update(find_query_ids(child))
    elif isinstance(value, list):
        for child in value:
            found.update(find_query_ids(child))
    elif isinstance(value, str) and "query_id" in value.lower():
        found.update(QUERY_ID.findall(value))
    return found


def trace_query_functions(trajectory: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    queries = {}
    for step in trajectory.get("steps", []):
        if not isinstance(step, dict):
            continue
        calls = {
            str(call.get("tool_call_id")): ai_functions(
                str(json_object(call.get("arguments")).get("sql") or "")
            )
            for call in step.get("tool_calls", [])
            if isinstance(call, dict)
        }
        observation = json_object(step.get("observation"))
        for result in observation.get("results", []):
            if not isinstance(result, dict):
                continue
            source = str(result.get("source_call_id"))
            if source in calls:
                for query_id in find_query_ids(result):
                    queries[query_id] = calls[source]
    return queries


def trace_query_models(trajectory: dict[str, Any]) -> dict[str, str]:
    queries = {}
    for step in trajectory.get("steps", []):
        if not isinstance(step, dict):
            continue
        calls = {
            str(call.get("tool_call_id")): ai_complete_model(
                str(json_object(call.get("arguments")).get("sql") or "")
            )
            for call in step.get("tool_calls", [])
            if isinstance(call, dict)
        }
        observation = json_object(step.get("observation"))
        for result in observation.get("results", []):
            if not isinstance(result, dict):
                continue
            model = calls.get(str(result.get("source_call_id")))
            if model:
                for query_id in find_query_ids(result):
                    queries[query_id] = model
    return queries


def agent_window(result: dict[str, Any]) -> tuple[datetime, datetime] | None:
    execution = json_object(result.get("agent_execution"))
    if not execution.get("started_at") or not execution.get("finished_at"):
        return None
    values = [
        datetime.fromisoformat(execution[key]) for key in ("started_at", "finished_at")
    ]
    return tuple(
        value.replace(tzinfo=UTC) if value.tzinfo is None else value for value in values
    )


def configured_agent_user(result: dict[str, Any], override: str | None) -> str | None:
    env = json_object(
        json_object(json_object(result.get("config")).get("agent")).get("env")
    )
    user = override or env.get("SNOWFLAKE_USER") or os.environ.get("SNOWFLAKE_USER")
    if isinstance(user, str) and user.startswith("${") and user.endswith("}"):
        user = os.environ.get(user[2:-1])
    return user if isinstance(user, str) and user.strip() else None


def assign_sessions(
    statements: list[dict[str, Any]],
    seeds: dict[str, set[str]],
    windows: dict[str, tuple[datetime, datetime]],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[int]]]:
    """Assign each Snowflake session to the one trial that issued it.

    Agents can spread SQL over pooled sessions or run it from Python, so many
    statements never appear in the trajectory. A session belongs to a trial when
    it contains one of that trial's trajectory query IDs, shares a Cortex Code
    request ID with such a session, or is a Cortex Code session lying entirely
    inside that trial's agent window. Sessions matching several trials, or none
    while holding AI work inside an agent window, are returned as ambiguous
    rather than dropped or guessed.
    """
    sessions: dict[int, list[dict[str, Any]]] = {}
    for row in statements:
        sessions.setdefault(int(row["SESSION_ID"]), []).append(row)
    owners = {
        session: {
            trial
            for trial, query_ids in seeds.items()
            if query_ids & {str(row["QUERY_ID"]) for row in rows}
        }
        for session, rows in sessions.items()
    }
    linked: dict[str, set[int]] = {}
    for session, rows in sessions.items():
        for row in rows:
            if row.get("REQUEST_ID"):
                linked.setdefault(str(row["REQUEST_ID"]), set()).add(session)
    changed = True
    while changed:
        changed = False
        for group in linked.values():
            combined = set().union(*(owners[session] for session in group))
            for session in group:
                if not combined <= owners[session]:
                    owners[session] |= combined
                    changed = True
    assigned: dict[str, list[dict[str, Any]]] = {trial: [] for trial in windows}
    ambiguous: dict[str, list[int]] = {trial: [] for trial in windows}
    for session, rows in sessions.items():
        candidates = owners[session]
        if not candidates and any(row.get("APP") == CORTEX_CODE_APP for row in rows):
            candidates = {
                trial
                for trial, (start, end) in windows.items()
                if all(start <= row["START_TIME"] <= end for row in rows)
            }
        if len(candidates) == 1:
            trial = next(iter(candidates))
            start, end = windows[trial]
            assigned[trial] += [
                row for row in rows if start <= row["START_TIME"] <= end
            ]
        elif any(
            ai_functions(str(row.get("QUERY_TEXT") or ""))
            or potentially_hidden_ai(str(row.get("QUERY_TEXT") or ""))
            for row in rows
        ):
            for trial, (start, end) in windows.items():
                if trial in candidates or any(
                    start <= row["START_TIME"] <= end for row in rows
                ):
                    ambiguous[trial].append(session)
    return assigned, ambiguous


def session_statements(
    connection: Any,
    seeds: dict[str, set[str]],
    windows: dict[str, tuple[datetime, datetime]],
    *,
    statement_timeout_seconds: int,
    agent_user: str | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[int]]]:
    """Read the collecting user's statements in the agent windows and assign them."""
    if not windows:
        return {}, {}
    cursor = connection.cursor(snowflake.connector.DictCursor)
    try:
        cursor.execute(
            """
            SELECT QUERY_ID, SESSION_ID, START_TIME, QUERY_TEXT,
                   TRY_PARSE_JSON(QUERY_TAG):app::STRING AS APP,
                   TRY_PARSE_JSON(QUERY_TAG):request_id::STRING AS REQUEST_ID
            FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
            WHERE USER_NAME = COALESCE(%s, CURRENT_USER())
              AND START_TIME BETWEEN %s AND %s
            """,
            (
                agent_user,
                min(start for start, _ in windows.values()),
                max(end for _, end in windows.values()),
            ),
            timeout=statement_timeout_seconds,
        )
        rows = cursor.fetchall()
    finally:
        cursor.close()
    return assign_sessions(rows, seeds, windows)


def add_session_statements(
    query_functions: dict[str, tuple[str, ...]],
    query_models: dict[str, str],
    statements: list[dict[str, Any]],
) -> None:
    """Attribute session statements, preferring server-side text over empty traces."""
    for row in statements:
        query_id = str(row["QUERY_ID"])
        query_text = str(row.get("QUERY_TEXT") or "")
        if not query_functions.get(query_id):
            query_functions[query_id] = ai_functions(query_text)
        if query_id not in query_models and (model := ai_complete_model(query_text)):
            query_models[query_id] = model


def trial_directories(paths: list[Path]) -> list[Path]:
    result: set[Path] = set()
    for path in paths:
        children = {item.parent for item in path.glob("*/result.json")}
        if children:
            result.update(children)
        elif (path / "result.json").is_file():
            result.add(path)
    if not result:
        raise RuntimeError("no Harbor trial result.json files found")
    return sorted(result)


def sibling_ai_queries(
    connection: Any,
    pending: dict[str, tuple[str, ...]],
    query_history_rows: list[dict[str, Any]],
    *,
    statement_timeout_seconds: int,
) -> dict[str, tuple[str, str | None]]:
    """Recover the metered statement preceding a multi-statement result ID."""
    history_by_id = {str(row.get("QUERY_ID") or ""): row for row in query_history_rows}
    targets = {
        query_id: history_by_id[query_id]
        for query_id in pending
        if query_id in history_by_id
        and not ai_functions(str(history_by_id[query_id].get("QUERY_TEXT") or ""))
    }
    if not targets:
        return {}

    session_histories: dict[int, list[dict[str, Any]]] = {}
    cursor = connection.cursor(snowflake.connector.DictCursor)
    try:
        for session_id in sorted(
            {
                int(row["SESSION_ID"])
                for row in targets.values()
                if row.get("SESSION_ID")
            }
        ):
            cursor.execute(
                f"""
                SELECT QUERY_ID, START_TIME, QUERY_TAG, QUERY_TEXT
                FROM TABLE(SNOWFLAKE.INFORMATION_SCHEMA.QUERY_HISTORY_BY_SESSION(
                    SESSION_ID => {session_id}, RESULT_LIMIT => 10000
                ))
                ORDER BY START_TIME
                """,
                timeout=statement_timeout_seconds,
            )
            session_histories[session_id] = cursor.fetchall()
    finally:
        cursor.close()

    recovered = {}
    for trace_query_id, target in targets.items():
        session_rows = session_histories.get(int(target["SESSION_ID"]), [])
        target_index = next(
            (
                index
                for index, row in enumerate(session_rows)
                if str(row.get("QUERY_ID") or "") == trace_query_id
            ),
            -1,
        )
        if target_index < 0:
            continue
        expected = pending[trace_query_id]
        query_tag = target.get("QUERY_TAG")
        candidates = (
            row
            for row in reversed(session_rows[max(0, target_index - 20) : target_index])
            if row.get("QUERY_TAG") == query_tag
            and ai_functions(str(row.get("QUERY_TEXT") or "")) == expected
        )
        match = next(candidates, None)
        if match:
            query_text = str(match.get("QUERY_TEXT") or "")
            recovered[trace_query_id] = (
                str(match["QUERY_ID"]),
                ai_complete_model(query_text),
            )
    return recovered


def snowflake_aisql_usage(
    connection: Any,
    query_functions: dict[str, tuple[str, ...]],
    *,
    query_models: dict[str, str] | None = None,
    unpaired_query_ids: set[str] | None = None,
    estimates: dict[str, dict[str, Any]] | None = None,
    statement_timeout_seconds: int = 300,
    recover_siblings: bool = True,
) -> tuple[list[TokenUsage], str, str | None, list[str], list[dict[str, Any]]]:
    estimates = estimates or {}
    query_models = query_models or {}
    unpaired_query_ids = unpaired_query_ids or set()
    billable = {
        query_id: functions
        for query_id, functions in query_functions.items()
        if functions
    }
    unpaired_reason = (
        "trajectory lacks SQL attribution for query IDs: "
        + ", ".join(sorted(unpaired_query_ids))
        if unpaired_query_ids
        else None
    )
    if not billable:
        source = "account_usage_no_agent_queries"
        return [], source, unpaired_reason, [], []
    placeholders = ",".join("%s" for _ in billable)
    cursor = connection.cursor(snowflake.connector.DictCursor)
    function_rows = []
    query_history_rows = []
    function_history_error = None
    query_history_error = None
    try:
        try:
            cursor.execute(
                f"""
                SELECT QUERY_ID, MODEL_NAME, TOKENS, TOKENS_GRANULAR
                FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_AISQL_USAGE_HISTORY
                WHERE QUERY_ID IN ({placeholders})
                """,
                tuple(sorted(billable)),
                timeout=statement_timeout_seconds,
            )
        except snowflake.connector.errors.Error as exc:
            if getattr(exc, "errno", None) in {2003, 2043, 3001}:
                return (
                    [],
                    "account_usage_unavailable",
                    "CORTEX_AISQL_USAGE_HISTORY is unavailable or not authorized",
                    sorted(billable),
                    [],
                )
            if getattr(exc, "errno", None) == 604:
                return (
                    [],
                    "account_usage_timeout",
                    "CORTEX_AISQL_USAGE_HISTORY exceeded the configured "
                    f"{statement_timeout_seconds}-second statement timeout",
                    sorted(billable),
                    [],
                )
            raise
        rows = cursor.fetchall()
        directly_reported = {
            str(row.get("QUERY_ID") or "")
            for row in rows
            if json_object(row.get("TOKENS_GRANULAR")) or row.get("TOKENS") is not None
        }
        fallback_ids = sorted(set(billable) - directly_reported)
        if fallback_ids:
            fallback_placeholders = ",".join("%s" for _ in fallback_ids)
            try:
                cursor.execute(
                    f"""
                    SELECT QUERY_ID, FUNCTION_NAME, MODEL_NAME, METRICS
                    FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_AI_FUNCTIONS_USAGE_HISTORY
                    WHERE QUERY_ID IN ({fallback_placeholders})
                    """,
                    tuple(fallback_ids),
                    timeout=statement_timeout_seconds,
                )
                function_rows = cursor.fetchall()
            except snowflake.connector.errors.Error as exc:
                if getattr(exc, "errno", None) in {2003, 2043, 3001, 604}:
                    function_history_error = (
                        "CORTEX_AI_FUNCTIONS_USAGE_HISTORY is unavailable"
                    )
                else:
                    raise
            try:
                cursor.execute(
                    f"""
                    SELECT QUERY_ID, SESSION_ID, START_TIME, QUERY_TAG, QUERY_TEXT,
                           ROWS_PRODUCED, ROWS_INSERTED
                    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
                    WHERE QUERY_ID IN ({fallback_placeholders})
                    """,
                    tuple(fallback_ids),
                    timeout=statement_timeout_seconds,
                )
                query_history_rows = cursor.fetchall()
            except snowflake.connector.errors.Error as exc:
                query_history_error = ledger_error("QUERY_HISTORY fallback", exc)
    finally:
        cursor.close()

    totals: dict[tuple[str, bool], list[int]] = {}
    represented: set[str] = set()
    partial_reasons = [unpaired_reason] if unpaired_reason else []
    if query_history_error:
        partial_reasons.append(query_history_error)
    attributions: list[dict[str, Any]] = []
    for row in rows:
        query_id = str(row.get("QUERY_ID") or "")
        functions = billable.get(query_id, ())
        model = str(row.get("MODEL_NAME") or "unknown")
        if model.lower() in {"default", "unknown"} and query_id in query_models:
            model = query_models[query_id]
        granular = json_object(row.get("TOKENS_GRANULAR"))
        model_metrics = (
            granular.get(model) if isinstance(granular.get(model), dict) else granular
        )
        if model_metrics:
            usage = normalize_usage(model, model_metrics)
            add_usage(totals, usage)
            attributions.append(
                {
                    "query_id": query_id,
                    "functions": functions,
                    "attribution": "reported_input_output",
                    "usage": usage.to_dict(),
                }
            )
        elif row.get("TOKENS") is not None:
            total_tokens = int(row["TOKENS"])
            function = functions[0] if len(functions) == 1 else None
            if "input_fraction" in estimates.get(function, {}):
                usage = estimated_usage(function, total_tokens, estimates)
                add_usage(totals, usage)
                attributions.append(
                    {
                        "query_id": query_id,
                        "functions": functions,
                        "attribution": "estimated_from_total",
                        "reported_total_tokens": total_tokens,
                        "usage": usage.to_dict(),
                    }
                )
            else:
                label = ", ".join(functions) or "unknown function"
                partial_reasons.append(
                    f"{query_id} exposes total tokens without an estimate for {label}"
                )
                attributions.append(
                    {
                        "query_id": query_id,
                        "functions": functions,
                        "attribution": "unconfigured_total",
                        "reported_total_tokens": total_tokens,
                    }
                )
        if model_metrics or row.get("TOKENS") is not None:
            represented.add(query_id)

    named_functions_by_id: dict[str, set[str]] = {}
    for row in function_rows:
        query_id = str(row.get("QUERY_ID") or "")
        name = re.sub(
            r"[^A-Z0-9]+", "_", str(row.get("FUNCTION_NAME") or "").upper()
        ).strip("_")
        if name and name != "DEFAULT":
            name = name if name.startswith("AI_") else f"AI_{name}"
            named_functions_by_id.setdefault(query_id, set()).add(name)
    represented_functions: dict[str, set[str]] = {}
    for row in function_rows:
        query_id = str(row.get("QUERY_ID") or "")
        reported_function = re.sub(
            r"[^A-Z0-9]+", "_", str(row.get("FUNCTION_NAME") or "").upper()
        ).strip("_")
        paired_functions = billable.get(query_id, ())
        if reported_function == "DEFAULT":
            unnamed = set(paired_functions) - named_functions_by_id.get(query_id, set())
            if len(unnamed) != 1:
                continue
            function = unnamed.pop()
        else:
            function = reported_function
            function = function if function.startswith("AI_") else f"AI_{function}"
            if len(paired_functions) == 1:
                function = paired_functions[0]
        functions = (function,)
        metrics = row.get("METRICS")
        input_tokens = metric_amount(metrics, "input", "tokens")
        output_tokens = metric_amount(metrics, "output", "tokens")
        cache_read_tokens = metric_amount(metrics, "cache_read_input", "tokens")
        cache_write_tokens = metric_amount(metrics, "cache_write_input", "tokens")
        total_tokens = metric_amount(metrics, "total", "tokens")
        pages = metric_amount(metrics, "input", "pages")
        if any(
            value is not None
            for value in (
                input_tokens,
                output_tokens,
                cache_read_tokens,
                cache_write_tokens,
            )
        ):
            model = str(row.get("MODEL_NAME") or "unknown")
            if model.lower() in {"default", "unknown"} and query_id in query_models:
                model = query_models[query_id]
            usage = TokenUsage(
                model_name=model,
                input_tokens=input_tokens or 0,
                cache_read_tokens=cache_read_tokens or 0,
                cache_write_tokens=cache_write_tokens or 0,
                output_tokens=output_tokens or 0,
            )
            attribution = "reported_input_output"
        elif total_tokens is not None and "input_fraction" in estimates.get(
            function, {}
        ):
            usage = estimated_usage(function, total_tokens, estimates)
            attribution = "estimated_from_total"
        elif pages is not None and "input_tokens_per_page" in estimates.get(
            function, {}
        ):
            usage = estimated_page_usage(function, pages, estimates)
            attribution = "estimated_from_pages"
        else:
            partial_reasons.append(
                f"{query_id} exposes unsupported usage metrics for {function}"
            )
            attributions.append(
                {
                    "query_id": query_id,
                    "functions": functions,
                    "attribution": "unconfigured_metrics",
                }
            )
            represented_functions.setdefault(query_id, set()).add(function)
            continue
        add_usage(totals, usage)
        detail = {
            "query_id": query_id,
            "functions": functions,
            "attribution": attribution,
            "usage": usage.to_dict(),
        }
        if total_tokens is not None:
            detail["reported_total_tokens"] = total_tokens
        if pages is not None:
            detail["reported_pages"] = pages
        attributions.append(detail)
        represented_functions.setdefault(query_id, set()).add(function)

    represented.update(
        query_id
        for query_id, functions in billable.items()
        if set(functions) <= represented_functions.get(query_id, set())
    )

    history_by_id = {str(row.get("QUERY_ID") or ""): row for row in query_history_rows}
    history_estimates = 0
    for query_id in sorted(set(billable) - represented):
        functions = billable[query_id]
        estimate = estimates.get("AI_PARSE_DOCUMENT", {})
        history = history_by_id.get(query_id)
        if (
            functions != ("AI_PARSE_DOCUMENT",)
            or history is None
            or "pages_per_document" not in estimate
        ):
            continue
        documents = max(
            int(history.get("ROWS_PRODUCED") or 0),
            int(history.get("ROWS_INSERTED") or 0),
        )
        pages = round(documents * float(estimate["pages_per_document"]))
        usage = estimated_page_usage("AI_PARSE_DOCUMENT", pages, estimates)
        add_usage(totals, usage)
        attributions.append(
            {
                "query_id": query_id,
                "functions": functions,
                "attribution": "estimated_from_document_rows",
                "reported_documents": documents,
                "estimated_pages": pages,
                "usage": usage.to_dict(),
            }
        )
        represented.add(query_id)
        history_estimates += 1

    sibling_recoveries = {}
    pending = sorted(set(billable) - represented)
    if recover_siblings and pending:
        try:
            sibling_recoveries = sibling_ai_queries(
                connection,
                {query_id: billable[query_id] for query_id in pending},
                query_history_rows,
                statement_timeout_seconds=statement_timeout_seconds,
            )
        except snowflake.connector.errors.Error as exc:
            partial_reasons.append(ledger_error("sibling query history", exc))
        if sibling_recoveries:
            aliases: dict[str, list[str]] = {}
            for trace_query_id, (actual_query_id, _) in sibling_recoveries.items():
                aliases.setdefault(actual_query_id, []).append(trace_query_id)
                if actual_query_id in represented:
                    represented.add(trace_query_id)
            for detail in attributions:
                if traces := aliases.get(str(detail["query_id"])):
                    detail["trace_query_ids"] = sorted(traces)
                    detail["query_id_recovery"] = "preceding_session_statement"
            recovered_functions = {
                actual_query_id: billable[trace_query_id]
                for trace_query_id, (actual_query_id, _) in sibling_recoveries.items()
                if actual_query_id not in represented
            }
            recovered_models = {}
            for trace_query_id, (
                actual_query_id,
                recovered_model,
            ) in sibling_recoveries.items():
                model = recovered_model or query_models.get(trace_query_id)
                if model:
                    recovered_models[actual_query_id] = model
            (
                recovered_usage,
                recovered_source,
                recovered_reason,
                _,
                recovered_attributions,
            ) = snowflake_aisql_usage(
                connection,
                recovered_functions,
                query_models=recovered_models,
                estimates=estimates,
                statement_timeout_seconds=statement_timeout_seconds,
                recover_siblings=False,
            )
            for usage in recovered_usage:
                add_usage(totals, usage)
            for detail in recovered_attributions:
                trace_query_ids = aliases.get(str(detail["query_id"]), [])
                if trace_query_ids:
                    detail["trace_query_ids"] = sorted(trace_query_ids)
                    if len(trace_query_ids) == 1:
                        detail["trace_query_id"] = trace_query_ids[0]
                    detail["query_id_recovery"] = "preceding_session_statement"
                    if detail["attribution"] != "not_yet_visible":
                        represented.update(trace_query_ids)
                attributions.append(detail)
            if recovered_reason:
                partial_reasons.append(recovered_reason)
            if recovered_source:
                history_estimates += 1

    pending = sorted(set(billable) - represented)
    if pending:
        if function_history_error:
            partial_reasons.append(function_history_error)
        partial_reasons.append(
            "usage not yet visible for query IDs: " + ", ".join(pending)
        )
        attributions.extend(
            {
                "query_id": query_id,
                "functions": billable[query_id],
                "attribution": "not_yet_visible",
            }
            for query_id in pending
        )
    return (
        usages_from_totals(totals),
        " + ".join(
            [
                "SNOWFLAKE.ACCOUNT_USAGE.CORTEX_AISQL_USAGE_HISTORY",
                *(["CORTEX_AI_FUNCTIONS_USAGE_HISTORY"] if function_rows else []),
                *(["QUERY_HISTORY"] if history_estimates else []),
            ]
        ),
        "; ".join(partial_reasons) or None,
        sorted(billable),
        sorted(attributions, key=lambda item: item["query_id"]),
    )


def connect(connection_name: str | None) -> Any:
    if connection_name:
        return snowflake.connector.connect(connection_name=connection_name)
    return snowflake.connector.connect()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("trials", nargs="+", type=Path)
    parser.add_argument("--connection")
    parser.add_argument(
        "--aisql-estimates", type=Path, default=ROOT / "configs/catc.json"
    )
    parser.add_argument(
        "--prices",
        type=Path,
        default=DEFAULT_PRICES,
        help="Provider price snapshot for monetary cost",
    )
    parser.add_argument("--statement-timeout-seconds", type=int, default=300)
    parser.add_argument("--agent-user", help="Snowflake user that executed agent SQL")
    parser.add_argument(
        "--minimum-ledger-age-seconds",
        type=int,
        default=DEFAULT_LEDGER_LAG_SECONDS,
        help="Minimum age of an agent window before Account Usage can be complete",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.statement_timeout_seconds <= 0:
        parser.error("--statement-timeout-seconds must be positive")
    if args.minimum_ledger_age_seconds < 0:
        parser.error("--minimum-ledger-age-seconds cannot be negative")
    collected_at = datetime.now(UTC)
    snapshot = load_snapshot(ROOT / "src/avalanche_bench/resources/arena_snapshot.json")
    estimates = load_aisql_estimates(args.aisql_estimates)
    prices = load_prices(args.prices)
    connection = connect(args.connection)
    try:
        trials = {}
        for trial in trial_directories(args.trials):
            result = json.loads((trial / "result.json").read_text(encoding="utf-8"))
            trajectory_path = trial / "agent/trajectory.json"
            trajectory = (
                json.loads(trajectory_path.read_text(encoding="utf-8"))
                if trajectory_path.is_file()
                else {}
            )
            trials[trial] = (result, trajectory)
        windows = {
            str(trial.resolve()): window
            for trial, (result, _) in trials.items()
            if (window := agent_window(result))
        }
        agent_users = {
            str(trial.resolve()): configured_agent_user(result, args.agent_user)
            for trial, (result, _) in trials.items()
        }
        session_rows: dict[str, list[dict[str, Any]]] = {}
        ambiguous_sessions: dict[str, list[int]] = {}
        session_errors: dict[str, str] = {}
        for user in sorted(set(agent_users.values()), key=lambda value: value or ""):
            group_windows = {
                key: value for key, value in windows.items() if agent_users[key] == user
            }
            seeds = {
                str(trial.resolve()): find_query_ids(trajectory)
                for trial, (_, trajectory) in trials.items()
                if str(trial.resolve()) in group_windows
            }
            try:
                assigned, ambiguous = session_statements(
                    connection,
                    seeds,
                    group_windows,
                    statement_timeout_seconds=args.statement_timeout_seconds,
                    agent_user=user,
                )
                session_rows.update(assigned)
                ambiguous_sessions.update(ambiguous)
            except snowflake.connector.errors.Error as exc:
                for key in group_windows:
                    session_errors[key] = ledger_error("session query history", exc)
        output = []
        for trial, (result, trajectory) in trials.items():
            trial_key = str(trial.resolve())
            agent_usage, agent_source, agent_reason = harbor_agent_usage(
                result, trial / "agent/cortex-code.txt"
            )
            query_ids = find_query_ids(trajectory)
            query_functions = trace_query_functions(trajectory)
            query_models = trace_query_models(trajectory)
            add_session_statements(
                query_functions, query_models, session_rows.get(trial_key, [])
            )
            (
                aisql_usage,
                aisql_source,
                aisql_reason,
                billable_ids,
                aisql_attributions,
            ) = ([], "account_usage_unavailable", None, [], [])
            try:
                (
                    aisql_usage,
                    aisql_source,
                    aisql_reason,
                    billable_ids,
                    aisql_attributions,
                ) = snowflake_aisql_usage(
                    connection,
                    query_functions,
                    query_models=query_models,
                    unpaired_query_ids=query_ids - set(query_functions),
                    estimates=estimates,
                    statement_timeout_seconds=args.statement_timeout_seconds,
                )
            except snowflake.connector.errors.Error as exc:
                aisql_reason = ledger_error("AISQL ledger", exc)
            coverage = coverage_reasons(
                window=windows.get(trial_key),
                statements=session_rows.get(trial_key, []),
                query_ids=query_ids,
                agent_user=agent_users[trial_key],
                collected_at=collected_at,
                minimum_ledger_age_seconds=args.minimum_ledger_age_seconds,
            )
            reasons = [aisql_reason, session_errors.get(trial_key), *coverage]
            if ambiguous := ambiguous_sessions.get(trial_key):
                reasons.append(
                    "AI statements in sessions not attributable to a single trial: "
                    + ", ".join(map(str, sorted(ambiguous)))
                )
            aisql_reason = "; ".join(filter(None, reasons)) or None
            agent = measure_component(
                "agent", agent_usage, snapshot, partial_reason=agent_reason
            )
            aisql = measure_component(
                "aisql",
                aisql_usage,
                snapshot,
                partial_reason=aisql_reason,
            )
            output.append(
                {
                    "trial": trial.name,
                    "agent_user": agent_users[trial_key],
                    "sql_coverage_reasons": coverage,
                    "agent_usage_source": agent_source,
                    "aisql_usage_source": aisql_source,
                    "agent_query_ids": sorted(query_ids),
                    "agent_session_ids": sorted(
                        {
                            int(row["SESSION_ID"])
                            for row in session_rows.get(trial_key, [])
                        }
                    ),
                    "billable_aisql_query_ids": billable_ids,
                    "aisql_query_attribution": aisql_attributions,
                    **aggregate_catc(agent, aisql),
                    "monetary_cost": aggregate_monetary_cost(
                        measure_cost_component(
                            "agent", agent_usage, prices, partial_reason=agent_reason
                        ),
                        measure_cost_component(
                            "aisql", aisql_usage, prices, partial_reason=aisql_reason
                        ),
                    ),
                }
            )
    finally:
        connection.close()
    payload = {
        "schema_version": "0.1.0",
        "collected_at": collected_at.isoformat(),
        "minimum_ledger_age_seconds": args.minimum_ledger_age_seconds,
        "arena_snapshot": snapshot["source"],
        "aisql_estimates": estimates,
        "price_snapshot": prices["source"],
        "trials": output,
        "judge_calls_included": False,
    }
    write_bytes_atomically(args.output, (json.dumps(payload, indent=2) + "\n").encode())
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
