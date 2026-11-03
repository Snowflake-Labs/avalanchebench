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

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from snowflake.connector.errors import ProgrammingError

from avalanche_bench.catc import TokenUsage

SCRIPT = Path(__file__).parents[1] / "scripts/collect_snowflake_usage.py"
SPEC = importlib.util.spec_from_file_location("collect_snowflake_usage", SCRIPT)
assert SPEC and SPEC.loader
collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collector)


def test_estimate_config_pins_function_ratios_and_page_conversion() -> None:
    estimates = collector.load_aisql_estimates(SCRIPT.parents[1] / "configs/catc.json")

    assert estimates["AI_CLASSIFY"]["input_fraction"] == 0.99
    assert estimates["AI_FILTER"]["input_fraction"] == 0.99
    assert estimates["AI_AGG"]["input_fraction"] == 0.95
    assert estimates["AI_SENTIMENT"]["input_fraction"] == 0.95
    assert estimates["AI_PARSE_DOCUMENT"] == {
        "reference_model": "claude-sonnet-4-6",
        "input_tokens_per_page": 2000,
        "output_tokens_per_page": 600,
        "pages_per_document": 1.6467,
    }


def test_harbor_aggregate_is_an_explicit_cache_lower_bound() -> None:
    result = {
        "agent_info": {"model_info": {"name": "claude-opus-4-8"}},
        "agent_result": {
            "n_input_tokens": 100,
            "n_cache_tokens": 70,
            "n_output_tokens": 10,
        },
    }
    usage, source, reason = collector.harbor_agent_usage(result)
    assert usage == [TokenUsage("claude-opus-4-8", 30, 70, 0, 10)]
    assert source == "harbor_result_aggregate_fallback"
    assert "lower read weight" in reason


def test_harbor_detailed_usage_preserves_cache_fields() -> None:
    result = {
        "agent_info": {"model_info": {"name": "claude-sonnet-5"}},
        "agent_result": {
            "metadata": {
                "usage": {
                    "input_tokens": 10,
                    "cache_read_input_tokens": 20,
                    "cache_creation_input_tokens": 30,
                    "output_tokens": 40,
                }
            }
        },
    }
    usage, source, reason = collector.harbor_agent_usage(result)
    assert usage == [TokenUsage("claude-sonnet-5", 10, 20, 30, 40)]
    assert source == "harbor_result_detailed_usage"
    assert reason is None


def test_cortex_code_log_recovers_exact_cache_split(tmp_path: Path) -> None:
    log = tmp_path / "cortex-code.txt"
    log.write_text(
        '{"type":"result","usage":{"input_tokens":10,'
        '"cache_read_input_tokens":20,"cache_creation_input_tokens":30,'
        '"output_tokens":40}}\n'
    )
    result = {
        "agent_info": {"model_info": {"name": "claude-sonnet-5"}},
        "agent_result": {
            "n_input_tokens": 60,
            "n_cache_tokens": 50,
            "n_output_tokens": 40,
        },
    }
    usage, source, reason = collector.harbor_agent_usage(result, log)
    assert usage == [TokenUsage("claude-sonnet-5", 10, 20, 30, 40)]
    assert source == "cortex_code_result_event"
    assert reason is None


def test_query_id_extraction_is_structural() -> None:
    query_id = "01c70712-091a-3ba4-0001-dd92417d3c93"
    value = {"query_id": query_id, "session_id": "109bcac5-d8b0-486c-bd66-a1626d1efb0a"}
    assert collector.find_query_ids(value) == {query_id}


def test_trace_pairs_query_id_with_ai_function() -> None:
    query_id = "01c70712-091a-3ba4-0001-dd92417d3c93"
    trajectory = {
        "steps": [
            {
                "tool_calls": [
                    {
                        "tool_call_id": "call-1",
                        "arguments": {"sql": "SELECT AI_FILTER(text, 'relevant')"},
                    }
                ],
                "observation": {
                    "results": [
                        {
                            "source_call_id": "call-1",
                            "content": f'{{"query_id": "{query_id}"}}',
                        }
                    ]
                },
            }
        ]
    }
    assert collector.trace_query_functions(trajectory) == {query_id: ("AI_FILTER",)}


def test_ai_function_extraction_ignores_count_tokens() -> None:
    query = (
        "SELECT AI_CLASSIFY(x), SNOWFLAKE.CORTEX.COMPLETE('m', x), AI_COUNT_TOKENS(x)"
    )
    assert collector.ai_functions(query) == ("AI_CLASSIFY", "AI_COMPLETE")


def test_ai_function_extraction_ignores_privilege_probe_literals() -> None:
    query = (
        "SELECT EXPLAIN_PRIVILEGES(statement => $$SELECT AI_CLASSIFY('x', "
        "['a','b'])$$, missing_only => true)"
    )
    assert collector.ai_functions(query) == ()


def test_trace_recovers_ai_complete_model() -> None:
    query_id = "01c70712-091a-3ba4-0001-dd92417d3c93"
    trajectory = {
        "steps": [
            {
                "tool_calls": [
                    {
                        "tool_call_id": "call-1",
                        "arguments": {
                            "sql": (
                                "SELECT AI_COMPLETE(model => 'gemini-3.1-pro', "
                                "prompt => 'x')"
                            )
                        },
                    }
                ],
                "observation": {
                    "results": [
                        {
                            "source_call_id": "call-1",
                            "content": f'{{"query_id": "{query_id}"}}',
                        }
                    ]
                },
            }
        ]
    }
    assert collector.trace_query_models(trajectory) == {query_id: "gemini-3.1-pro"}


def test_job_directory_expands_to_trials(tmp_path: Path) -> None:
    job = tmp_path / "job"
    (job / "result.json").parent.mkdir()
    (job / "result.json").write_text("{}")
    trials = [job / "first", job / "second"]
    for trial in trials:
        trial.mkdir()
        (trial / "result.json").write_text("{}")
    assert collector.trial_directories([job]) == trials


def test_individual_trial_directory_is_preserved(tmp_path: Path) -> None:
    trial = tmp_path / "trial"
    trial.mkdir()
    (trial / "result.json").write_text("{}")
    assert collector.trial_directories([trial]) == [trial]


def test_account_usage_timeout_retains_explicit_unavailable_status() -> None:
    class Cursor:
        timeout = None

        def execute(self, _sql, _params, *, timeout):
            self.timeout = timeout
            raise ProgrammingError(msg="query timed out", errno=604)

        def close(self):
            return None

    class Connection:
        cursor_instance = Cursor()

        def cursor(self, _cursor_type):
            return self.cursor_instance

    connection = Connection()
    usage, source, reason, billable, attribution = collector.snowflake_aisql_usage(
        connection,
        {"01c70712-091a-3ba4-0001-dd92417d3c93": ("AI_COMPLETE",)},
        statement_timeout_seconds=7,
    )
    assert usage == []
    assert source == "account_usage_timeout"
    assert reason == (
        "CORTEX_AISQL_USAGE_HISTORY exceeded the configured 7-second statement timeout"
    )
    assert billable == ["01c70712-091a-3ba4-0001-dd92417d3c93"]
    assert attribution == []
    assert connection.cursor_instance.timeout == 7


def test_total_tokens_are_estimated_by_function() -> None:
    classify_id = "01c70712-091a-3ba4-0001-dd92417d3c93"
    complete_id = "01c70712-091a-3ba4-0001-dd92417d3c94"

    class Cursor:
        def execute(self, _sql, _params, *, timeout):
            return None

        def fetchall(self):
            return [
                {
                    "QUERY_ID": classify_id,
                    "MODEL_NAME": "unknown",
                    "TOKENS": 1000,
                    "TOKENS_GRANULAR": None,
                },
                {
                    "QUERY_ID": complete_id,
                    "MODEL_NAME": "claude-sonnet-4-6",
                    "TOKENS": 30,
                    "TOKENS_GRANULAR": {
                        "input_tokens": 10,
                        "output_tokens": 20,
                    },
                },
            ]

        def close(self):
            return None

    class Connection:
        cursor_instance = Cursor()

        def cursor(self, _cursor_type):
            return self.cursor_instance

    usage, source, reason, billable, attribution = collector.snowflake_aisql_usage(
        Connection(),
        {
            classify_id: ("AI_CLASSIFY",),
            complete_id: ("AI_COMPLETE",),
        },
        estimates={
            "AI_CLASSIFY": {
                "reference_model": "llama3.3-70b",
                "input_fraction": 0.999,
            }
        },
    )
    assert usage == [
        TokenUsage("claude-sonnet-4-6", 10, 0, 0, 20),
        TokenUsage("llama3.3-70b", 999, 0, 0, 1, estimated=True),
    ]
    assert source == "SNOWFLAKE.ACCOUNT_USAGE.CORTEX_AISQL_USAGE_HISTORY"
    assert reason is None
    assert billable == [classify_id, complete_id]
    assert [item["attribution"] for item in attribution] == [
        "estimated_from_total",
        "reported_input_output",
    ]


def test_function_history_estimates_total_tokens_and_pages() -> None:
    classify_id = "01c70712-091a-3ba4-0001-dd92417d3c93"
    parse_id = "01c70712-091a-3ba4-0001-dd92417d3c94"
    complete_id = "01c70712-091a-3ba4-0001-dd92417d3c95"
    historical_parse_id = "01c70712-091a-3ba4-0001-dd92417d3c96"

    class Cursor:
        rows = []

        def execute(self, sql, _params, *, timeout):
            if "CORTEX_AI_FUNCTIONS_USAGE_HISTORY" in sql:
                self.rows = [
                    {
                        "QUERY_ID": classify_id,
                        "FUNCTION_NAME": "default",
                        "MODEL_NAME": "default",
                        "METRICS": [
                            {
                                "key": {"metric": "total", "unit": "tokens"},
                                "value": 1000,
                            }
                        ],
                    },
                    {
                        "QUERY_ID": parse_id,
                        "FUNCTION_NAME": "AI_PARSE_DOCUMENT",
                        "MODEL_NAME": "layout",
                        "METRICS": (
                            '[{"key":{"metric":"input","unit":"pages"},"value":2}]'
                        ),
                    },
                    {
                        "QUERY_ID": parse_id,
                        "FUNCTION_NAME": "AI_PARSE_DOCUMENT",
                        "MODEL_NAME": "layout",
                        "METRICS": [
                            {
                                "key": {"metric": "input", "unit": "pages"},
                                "value": 3,
                            }
                        ],
                    },
                    {
                        "QUERY_ID": complete_id,
                        "FUNCTION_NAME": "AI Complete",
                        "MODEL_NAME": "default",
                        "METRICS": [
                            {
                                "key": {"metric": "input", "unit": "tokens"},
                                "value": 100,
                            },
                            {
                                "key": {"metric": "output", "unit": "tokens"},
                                "value": 20,
                            },
                        ],
                    },
                    {
                        "QUERY_ID": complete_id,
                        "FUNCTION_NAME": "AI_COMPLETE",
                        "MODEL_NAME": "default",
                        "METRICS": [
                            {
                                "key": {"metric": "cache_read_input", "unit": "tokens"},
                                "value": 30,
                            }
                        ],
                    },
                ]
            elif "QUERY_HISTORY" in sql:
                self.rows = [
                    {
                        "QUERY_ID": historical_parse_id,
                        "ROWS_PRODUCED": 2,
                        "ROWS_INSERTED": 0,
                    }
                ]
            else:
                self.rows = []

        def fetchall(self):
            return self.rows

        def close(self):
            return None

    class Connection:
        def cursor(self, _cursor_type):
            return Cursor()

    usage, source, reason, billable, attribution = collector.snowflake_aisql_usage(
        Connection(),
        {
            classify_id: ("AI_CLASSIFY",),
            parse_id: ("AI_PARSE_DOCUMENT",),
            complete_id: ("AI_COMPLETE",),
            historical_parse_id: ("AI_PARSE_DOCUMENT",),
        },
        query_models={complete_id: "openai-gpt-4.1"},
        estimates={
            "AI_CLASSIFY": {
                "reference_model": "llama3.3-70b",
                "input_fraction": 0.99,
            },
            "AI_PARSE_DOCUMENT": {
                "reference_model": "claude-sonnet-4-6",
                "input_tokens_per_page": 2000,
                "output_tokens_per_page": 600,
                "pages_per_document": 1.6467,
            },
        },
    )

    assert usage == [
        TokenUsage("claude-sonnet-4-6", 16_000, 0, 0, 4_800, estimated=True),
        TokenUsage("llama3.3-70b", 990, 0, 0, 10, estimated=True),
        TokenUsage("openai-gpt-4.1", 100, 30, 0, 20),
    ]
    assert "CORTEX_AI_FUNCTIONS_USAGE_HISTORY" in source
    assert source.endswith("QUERY_HISTORY")
    assert reason is None
    assert billable == [classify_id, parse_id, complete_id, historical_parse_id]
    assert [item["attribution"] for item in attribution] == [
        "estimated_from_total",
        "estimated_from_pages",
        "estimated_from_pages",
        "reported_input_output",
        "reported_input_output",
        "estimated_from_document_rows",
    ]
    assert sum(item.get("reported_pages", 0) for item in attribution) == 5
    assert sum(item.get("estimated_pages", 0) for item in attribution) == 3


def test_multi_function_default_row_uses_unreported_function_split() -> None:
    query_id = "01c70712-091a-3ba4-0001-dd92417d3c93"

    class Cursor:
        rows = []

        def execute(self, sql, _params, *, timeout):
            if "CORTEX_AI_FUNCTIONS_USAGE_HISTORY" in sql:
                self.rows = [
                    {
                        "QUERY_ID": query_id,
                        "FUNCTION_NAME": "default",
                        "MODEL_NAME": "default",
                        "METRICS": [
                            {
                                "key": {"metric": "total", "unit": "tokens"},
                                "value": 1000,
                            }
                        ],
                    },
                    {
                        "QUERY_ID": query_id,
                        "FUNCTION_NAME": "AI_CLASSIFY",
                        "MODEL_NAME": "",
                        "METRICS": [
                            {
                                "key": {"metric": "total", "unit": "tokens"},
                                "value": 2000,
                            }
                        ],
                    },
                ]
            else:
                self.rows = []

        def fetchall(self):
            return self.rows

        def close(self):
            return None

    class Connection:
        def cursor(self, _cursor_type):
            return Cursor()

    values, _, reason, _, attribution = collector.snowflake_aisql_usage(
        Connection(),
        {query_id: ("AI_CLASSIFY", "AI_SENTIMENT")},
        estimates={
            "AI_CLASSIFY": {
                "reference_model": "llama3.3-70b",
                "input_fraction": 0.99,
            },
            "AI_SENTIMENT": {
                "reference_model": "llama3.3-70b",
                "input_fraction": 0.95,
            },
        },
        recover_siblings=False,
    )

    assert values == [TokenUsage("llama3.3-70b", 2930, 0, 0, 70, estimated=True)]
    assert reason is None
    assert {tuple(item["functions"]) for item in attribution} == {
        ("AI_CLASSIFY",),
        ("AI_SENTIMENT",),
    }


def test_sibling_ai_query_recovers_preceding_statement() -> None:
    ai_query_id = "01c70711-091a-3ba4-0001-dd92417d3c92"
    result_query_id = "01c70712-091a-3ba4-0001-dd92417d3c93"

    class Cursor:
        def execute(self, sql, *, timeout):
            self.rows = [
                {
                    "QUERY_ID": ai_query_id,
                    "QUERY_TAG": "run-1",
                    "QUERY_TEXT": "SELECT AI_SENTIMENT(comment_text) FROM comments",
                },
                {
                    "QUERY_ID": result_query_id,
                    "QUERY_TAG": "run-1",
                    "QUERY_TEXT": "SELECT COUNT(*) FROM classified",
                },
            ]

        def fetchall(self):
            return self.rows

        def close(self):
            return None

    class Connection:
        def cursor(self, _cursor_type):
            return Cursor()

    recovered = collector.sibling_ai_queries(
        Connection(),
        {result_query_id: ("AI_SENTIMENT",)},
        [
            {
                "QUERY_ID": result_query_id,
                "SESSION_ID": 42,
                "QUERY_TAG": "run-1",
                "QUERY_TEXT": "SELECT COUNT(*) FROM classified",
            }
        ],
        statement_timeout_seconds=300,
    )

    assert recovered == {result_query_id: (ai_query_id, None)}


def test_sessions_attribute_untraced_statements_to_one_trial() -> None:
    def at(minute: int) -> datetime:
        return datetime(2026, 10, 4, 8, minute, tzinfo=UTC)

    def row(query_id, session, minute, text="SELECT 1", request=None):
        return {
            "QUERY_ID": query_id,
            "SESSION_ID": session,
            "START_TIME": at(minute),
            "QUERY_TEXT": text,
            "REQUEST_ID": request,
        }

    ai = "SELECT AI_CLASSIFY(review_text, ['a', 'b']) FROM reviews"
    statements = [
        row("traced", 1, 5, request="req-a"),
        row("pooled", 2, 6, ai, request="req-a"),
        row("python", 3, 7, ai),
        row("late", 3, 31, ai),
        row("other-traced", 4, 15),
        row("overlap", 5, 18, ai),
        row("overlap-plain", 6, 18),
    ]
    windows = {"a": (at(0), at(20)), "b": (at(10), at(30))}

    assigned, ambiguous = collector.assign_sessions(
        statements, {"a": {"traced"}, "b": {"other-traced"}}, windows
    )

    assert [row["QUERY_ID"] for row in assigned["a"]] == ["traced", "pooled"]
    assert [row["QUERY_ID"] for row in assigned["b"]] == ["other-traced"]
    assert ambiguous == {"a": [3, 5], "b": [5]}


def test_agent_window_assigns_only_cortex_code_sessions() -> None:
    start = datetime(2026, 10, 4, 8, 0, tzinfo=UTC)

    def statement(query_id, session, app):
        return {
            "QUERY_ID": query_id,
            "SESSION_ID": session,
            "START_TIME": start + timedelta(minutes=5),
            "QUERY_TEXT": "SELECT AI_SENTIMENT(comment_text) FROM comments",
            "APP": app,
            "REQUEST_ID": None,
        }

    agent = statement("agent", 7, "cortex_code_cli")
    other = statement("other", 8, None)

    assigned, ambiguous = collector.assign_sessions(
        [agent, other],
        {"only": set()},
        {"only": (start, start + timedelta(hours=1))},
    )

    assert assigned == {"only": [agent]}
    assert ambiguous == {"only": [8]}


def test_session_text_attributes_query_ids_traced_without_sql() -> None:
    query_functions = {"repl": (), "sql": ("AI_SENTIMENT",)}
    query_models: dict[str, str] = {}

    collector.add_session_statements(
        query_functions,
        query_models,
        [
            {
                "QUERY_ID": "repl",
                "QUERY_TEXT": "SELECT AI_COMPLETE('claude-sonnet-4-6', prompt) FROM t",
            },
            {"QUERY_ID": "sql", "QUERY_TEXT": "SELECT AI_SENTIMENT(text) FROM t"},
            {"QUERY_ID": "plain", "QUERY_TEXT": "SELECT 1"},
        ],
    )

    assert query_functions == {
        "repl": ("AI_COMPLETE",),
        "sql": ("AI_SENTIMENT",),
        "plain": (),
    }
    assert query_models == {"repl": "claude-sonnet-4-6"}


def test_session_attribution_and_sibling_recovery_count_one_physical_query() -> None:
    actual = "01c70712-091a-3ba4-0001-dd92417d3c93"
    final = "01c70712-091a-3ba4-0001-dd92417d3c94"

    class Cursor:
        def execute(self, sql, params=None, *, timeout):
            if "CORTEX_AISQL_USAGE_HISTORY" in sql:
                self.rows = [
                    {
                        "QUERY_ID": actual,
                        "MODEL_NAME": "llama3.3-70b",
                        "TOKENS": 120,
                        "TOKENS_GRANULAR": {"input_tokens": 100, "output_tokens": 20},
                    }
                ]
            elif "QUERY_HISTORY_BY_SESSION" in sql:
                self.rows = [
                    {
                        "QUERY_ID": actual,
                        "QUERY_TAG": "trial",
                        "QUERY_TEXT": "SELECT AI_COMPLETE('llama3.3-70b', 'x')",
                    },
                    {"QUERY_ID": final, "QUERY_TAG": "trial", "QUERY_TEXT": "SELECT 1"},
                ]
            elif "QUERY_HISTORY" in sql:
                self.rows = [
                    {
                        "QUERY_ID": final,
                        "SESSION_ID": 1,
                        "QUERY_TAG": "trial",
                        "QUERY_TEXT": "SELECT 1",
                    }
                ]
            else:
                self.rows = []

        def fetchall(self):
            return self.rows

        def close(self):
            pass

    class Connection:
        def cursor(self, _kind):
            return Cursor()

    functions = {final: ("AI_COMPLETE",)}
    collector.add_session_statements(
        functions,
        {},
        [{"QUERY_ID": actual, "QUERY_TEXT": "SELECT AI_COMPLETE('llama3.3-70b', 'x')"}],
    )
    usages, _, reason, _, attribution = collector.snowflake_aisql_usage(
        Connection(), functions
    )
    assert usages == [TokenUsage("llama3.3-70b", 100, 0, 0, 20)]
    assert reason is None
    assert len(attribution) == 1
    assert attribution[0]["trace_query_ids"] == [final]


def test_sql_coverage_requires_identity_freshness_and_observed_sessions() -> None:
    end = datetime(2026, 1, 1, tzinfo=UTC)
    common = dict(
        window=(end - timedelta(minutes=1), end),
        query_ids={"query"},
        collected_at=end + timedelta(hours=4),
        minimum_ledger_age_seconds=10800,
    )
    missing = collector.coverage_reasons(statements=[], agent_user=None, **common)
    assert any("identity unverified" in r for r in missing)
    assert any("zero AISQL usage is unverified" in r for r in missing)
    assert any("cover all trajectory" in r for r in missing)
    rows = [{"QUERY_ID": "query", "QUERY_TEXT": "SELECT 1"}]
    assert (
        collector.coverage_reasons(
            statements=rows, agent_user="benchmark_user", **common
        )
        == []
    )
    fresh = {**common, "collected_at": end + timedelta(minutes=1)}
    assert (
        "lag bound"
        in collector.coverage_reasons(
            statements=rows, agent_user="benchmark_user", **fresh
        )[0]
    )
    rows[0]["QUERY_TEXT"] = "CALL process_documents()"
    assert any(
        "dynamic SQL" in r
        for r in collector.coverage_reasons(
            statements=rows, agent_user="benchmark_user", **common
        )
    )


def test_agent_user_is_bound_in_session_history_query() -> None:
    class Cursor:
        params = None

        def execute(self, sql, params, *, timeout):
            assert "COALESCE(%s, CURRENT_USER())" in sql
            self.params = params

        def fetchall(self):
            return []

        def close(self):
            pass

    class Connection:
        instance = Cursor()

        def cursor(self, _kind):
            return self.instance

    c = Connection()
    at = datetime(2026, 1, 1, tzinfo=UTC)
    collector.session_statements(
        c,
        {"trial": set()},
        {"trial": (at, at)},
        statement_timeout_seconds=10,
        agent_user="benchmark_user",
    )
    assert c.instance.params[0] == "benchmark_user"


def test_main_preserves_agent_lower_bound_on_session_timeout(
    tmp_path, monkeypatch
) -> None:
    class Cursor:
        def execute(self, *_args, **_kwargs):
            raise ProgrammingError(msg="timeout", errno=604)

        def close(self):
            pass

    class Connection:
        def cursor(self, _kind):
            return Cursor()

        def close(self):
            pass

    trial = tmp_path / "trial"
    trial.mkdir()
    (trial / "result.json").write_text(
        json.dumps(
            {
                "agent_info": {"model_info": {"name": "llama3.3-70b"}},
                "agent_result": {
                    "metadata": {"usage": {"input_tokens": 100, "output_tokens": 20}}
                },
                "agent_execution": {
                    "started_at": "2026-01-01T00:00:00+00:00",
                    "finished_at": "2026-01-01T00:01:00+00:00",
                },
            }
        )
    )
    out = tmp_path / "catc.json"
    monkeypatch.setattr(collector, "connect", lambda _: Connection())
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "collector",
            str(trial),
            "--agent-user",
            "benchmark_user",
            "--output",
            str(out),
        ],
    )
    assert collector.main() == 0
    result = json.loads(out.read_text())["trials"][0]
    assert result["status"] == "partial_known_lower_bound"
    assert result["total_catc"] is None
    assert result["known_catc_lower_bound"] == 200
    assert "session query history unavailable" in result["components"][1]["reason"]
    cost = result["monetary_cost"]
    assert cost["status"] == "partial_known_lower_bound"
    assert cost["total_usd"] is None
    assert cost["agent_usd"] == pytest.approx((100 * 0.1 + 20 * 0.32) / 1e6)
    assert cost["known_usd_lower_bound"] == cost["agent_usd"]
    assert "session query history unavailable" in cost["components"][1]["reason"]
    assert json.loads(out.read_text())["price_snapshot"]["retrieved_at"] == (
        "2026-10-29"
    )


def test_main_does_not_call_empty_history_exact_zero(tmp_path, monkeypatch) -> None:
    class Cursor:
        def execute(self, *_args, **_kwargs):
            pass

        def fetchall(self):
            return []

        def close(self):
            pass

    class Connection:
        def cursor(self, _kind):
            return Cursor()

        def close(self):
            pass

    trial = tmp_path / "trial"
    trial.mkdir()
    (trial / "result.json").write_text(
        json.dumps(
            {
                "agent_info": {"model_info": {"name": "llama3.3-70b"}},
                "agent_result": {
                    "metadata": {"usage": {"input_tokens": 10, "output_tokens": 1}}
                },
                "agent_execution": {
                    "started_at": "2026-01-01T00:00:00",
                    "finished_at": "2026-01-01T00:01:00",
                },
            }
        )
    )
    out = tmp_path / "catc.json"
    monkeypatch.setattr(collector, "connect", lambda _: Connection())
    monkeypatch.setattr(sys, "argv", ["collector", str(trial), "--output", str(out)])
    assert collector.main() == 0
    result = json.loads(out.read_text())["trials"][0]
    assert result["status"] == "partial_known_lower_bound"
    assert result["aisql_catc"] is None
    assert result["total_catc"] is None
    assert any(
        "zero AISQL usage is unverified" in r for r in result["sql_coverage_reasons"]
    )
