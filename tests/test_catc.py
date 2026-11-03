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

import pytest

from avalanche_bench.catc import (
    CatcComponent,
    TokenUsage,
    aggregate_catc,
    measure_component,
    normalize_usage,
    usage_catc,
)


def snapshot() -> dict:
    return {
        "anchor_model": "anchor",
        "cortex_to_arena_model_name": {"agent": "rated", "aisql": "anchor"},
        "ratings": {"anchor": {"rating": 1200}, "rated": {"rating": 1600}},
    }


def test_catc_formula_and_component_boundary() -> None:
    usage = TokenUsage(
        "agent",
        input_tokens=100,
        cache_read_tokens=100,
        cache_write_tokens=100,
        output_tokens=100,
    )
    assert usage_catc(usage, snapshot()) == pytest.approx(7350.0)
    agent = measure_component("agent", [usage], snapshot())
    aisql = measure_component(
        "aisql", [TokenUsage("aisql", input_tokens=50)], snapshot()
    )
    total = aggregate_catc(agent, aisql)
    assert total["agent_catc"] == pytest.approx(7350.0)
    assert total["aisql_catc"] == pytest.approx(50.0)
    assert total["total_catc"] == pytest.approx(7400.0)
    assert total["agent_total_tokens"] == 400
    assert total["aisql_total_tokens"] == 50
    assert total["total_tokens"] == 450
    assert total["components"][0]["total_tokens"] == 400
    assert total["judge_calls_included"] is False


def test_cache_normalization_splits_prompt_tokens() -> None:
    usage = normalize_usage(
        "agent",
        {
            "prompt_tokens": 100,
            "cached_input_tokens": 30,
            "cache_write_tokens": 10,
            "completion_tokens": 20,
        },
    )
    assert usage == TokenUsage("agent", 60, 30, 10, 20)


def test_normalization_preserves_estimated_attribution() -> None:
    usage = normalize_usage(
        "aisql", {"input_tokens": 80, "output_tokens": 20, "estimated": True}
    )
    assert usage == TokenUsage("aisql", 80, 0, 0, 20, estimated=True)


def test_unknown_model_is_unavailable_without_losing_usage() -> None:
    usage = TokenUsage("hidden-model", input_tokens=100)
    result = measure_component("agent", [usage], snapshot())
    assert result.status == "unavailable"
    assert result.catc is None
    assert result.usage == (usage,)


def test_aisql_partial_cost_produces_lower_bound() -> None:
    agent = measure_component(
        "agent", [TokenUsage("agent", input_tokens=1)], snapshot()
    )
    aisql = measure_component(
        "aisql",
        [TokenUsage("aisql", input_tokens=10)],
        snapshot(),
        partial_reason="one page-metered function has no token usage",
    )
    total = aggregate_catc(agent, aisql)
    assert total["total_catc"] is None
    assert total["known_catc_lower_bound"] == pytest.approx(20.0)
    assert total["status"] == "partial_known_lower_bound"
    assert total["agent_total_tokens"] == 1
    assert total["aisql_total_tokens"] is None
    assert total["total_tokens"] is None


def test_estimated_usage_produces_a_labeled_total() -> None:
    agent = measure_component(
        "agent", [TokenUsage("agent", input_tokens=10)], snapshot()
    )
    aisql = measure_component(
        "aisql",
        [TokenUsage("aisql", input_tokens=80, output_tokens=20, estimated=True)],
        snapshot(),
    )
    total = aggregate_catc(agent, aisql)
    assert aisql.status == "estimated"
    assert aisql.catc == pytest.approx(180.0)
    assert aisql.known_catc_lower_bound == 0
    assert total["total_catc"] == pytest.approx(280.0)
    assert total["known_catc_lower_bound"] == pytest.approx(100.0)
    assert total["status"] == "estimated"
    assert total["agent_total_tokens"] == 10
    assert total["aisql_total_tokens"] == 100
    assert total["total_tokens"] == 110


def test_evaluator_component_cannot_enter_total() -> None:
    judge = CatcComponent(  # type: ignore[arg-type]
        "judge", "exact", 10.0, 10.0, (), None
    )
    aisql = measure_component("aisql", [], snapshot())
    with pytest.raises(ValueError, match="only agent"):
        aggregate_catc(judge, aisql)
