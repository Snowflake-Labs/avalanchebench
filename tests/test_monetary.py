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

import pytest

from avalanche_bench.catc import TokenUsage
from avalanche_bench.cli import main
from avalanche_bench.monetary import (
    CostComponent,
    aggregate_monetary_cost,
    load_prices,
    measure_cost_component,
    usage_cost,
)


def prices() -> dict:
    return {
        "source": {"retrieved_at": "test"},
        "unit": "USD per million tokens",
        "models": {
            "agent": {
                "input": 5.0,
                "cache_read": 0.5,
                "cache_write": 6.25,
                "output": 25.0,
            },
            "aisql": {
                "input": 0.1,
                "cache_read": None,
                "cache_write": None,
                "output": 0.32,
            },
        },
    }


def test_pinned_snapshot_uses_provider_list_prices() -> None:
    snapshot = load_prices()
    assert snapshot["source"]["retrieved_at"] == "2026-10-29"
    models = snapshot["models"]
    assert models["claude-opus-4-8"] | {"provider": None} == {
        "input": 5.0,
        "cache_read": 0.5,
        "cache_write": 6.25,
        "output": 25.0,
        "provider": None,
    }
    assert models["claude-sonnet-5"]["input"] == 2.0
    assert models["openai-gpt-5.6-sol"]["output"] == 20.0
    assert models["claude-sonnet-4-6"]["input"] == 3.0
    assert models["llama3.3-70b"]["cache_read"] is None


def test_every_configured_reference_model_has_a_price() -> None:
    from pathlib import Path

    estimates = json.loads(
        (Path(__file__).parents[1] / "configs/catc.json").read_text(encoding="utf-8")
    )
    models = load_prices()["models"]
    assert {item["reference_model"] for item in estimates.values()} <= set(models)


def test_cost_formula_prices_each_token_type() -> None:
    usage = TokenUsage(
        "agent",
        input_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        cache_write_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    assert usage_cost(usage, prices()) == pytest.approx(36.75)
    agent = measure_cost_component("agent", [usage], prices())
    aisql = measure_cost_component(
        "aisql", [TokenUsage("aisql", input_tokens=2_000_000)], prices()
    )
    total = aggregate_monetary_cost(agent, aisql)
    assert total["agent_usd"] == pytest.approx(36.75)
    assert total["aisql_usd"] == pytest.approx(0.2)
    assert total["total_usd"] == pytest.approx(36.95)
    assert total["status"] == "exact"
    assert total["judge_calls_included"] is False


def test_estimated_usage_produces_a_labeled_total() -> None:
    agent = measure_cost_component(
        "agent", [TokenUsage("agent", input_tokens=1_000_000)], prices()
    )
    aisql = measure_cost_component(
        "aisql",
        [TokenUsage("aisql", 800_000, 0, 0, 200_000, estimated=True)],
        prices(),
    )
    total = aggregate_monetary_cost(agent, aisql)
    assert aisql.status == "estimated"
    assert aisql.usd == pytest.approx(0.144)
    assert aisql.known_usd_lower_bound == 0
    assert total["status"] == "estimated"
    assert total["total_usd"] == pytest.approx(5.144)
    assert total["known_usd_lower_bound"] == pytest.approx(5.0)


def test_unpriced_model_or_token_type_is_a_lower_bound() -> None:
    priced = TokenUsage("agent", input_tokens=1_000_000)
    unknown = TokenUsage("hidden-model", input_tokens=100)
    cached = TokenUsage("aisql", input_tokens=1_000_000, cache_read_tokens=10)
    result = measure_cost_component("agent", [priced, unknown], prices())
    assert result.status == "partial_known_lower_bound"
    assert result.usd is None
    assert result.known_usd_lower_bound == pytest.approx(5.0)
    assert "price unavailable for 'hidden-model'" in result.reason
    result = measure_cost_component("aisql", [cached], prices())
    assert result.status == "unavailable"
    assert "cache_read price unavailable for 'aisql'" in result.reason


def test_partial_reason_withholds_total() -> None:
    agent = measure_cost_component(
        "agent", [TokenUsage("agent", input_tokens=1_000_000)], prices()
    )
    aisql = measure_cost_component(
        "aisql",
        [TokenUsage("aisql", input_tokens=1_000_000)],
        prices(),
        partial_reason="usage not yet visible",
    )
    total = aggregate_monetary_cost(agent, aisql)
    assert total["total_usd"] is None
    assert total["aisql_usd"] is None
    assert total["known_usd_lower_bound"] == pytest.approx(5.1)
    assert total["status"] == "partial_known_lower_bound"


def test_evaluator_component_cannot_enter_total() -> None:
    judge = CostComponent("judge", "exact", 1.0, 1.0)  # type: ignore[arg-type]
    aisql = measure_cost_component("aisql", [], prices())
    with pytest.raises(ValueError, match="only agent"):
        aggregate_monetary_cost(judge, aisql)


@pytest.mark.parametrize(
    "change",
    [
        {"unit": "USD per token"},
        {"models": {"agent": {"input": 1.0}}},
        {"models": {"agent": {**prices()["models"]["agent"], "output": -1}}},
        {"models": {"agent": {**prices()["models"]["agent"], "output": "1"}}},
    ],
)
def test_invalid_price_snapshot_is_rejected(tmp_path, change) -> None:
    path = tmp_path / "prices.json"
    path.write_text(json.dumps(prices() | change), encoding="utf-8")
    with pytest.raises(ValueError):
        load_prices(path)


def test_cost_command_writes_monetary_cost(tmp_path) -> None:
    agent = tmp_path / "agent.json"
    aisql = tmp_path / "aisql.json"
    out = tmp_path / "cost.json"
    agent.write_text(
        json.dumps(
            {
                "usage": [
                    {
                        "model": "claude-opus-4-8",
                        "input_tokens": 1_000_000,
                        "cache_read_input_tokens": 2_000_000,
                        "output_tokens": 100_000,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    aisql.write_text(
        json.dumps(
            {
                "usage": [
                    {
                        "model": "mistral-large2",
                        "input_tokens": 1_000_000,
                        "output_tokens": 1_000_000,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    assert (
        main(
            [
                "cost",
                "--agent-usage",
                str(agent),
                "--aisql-usage",
                str(aisql),
                "--output",
                str(out),
            ]
        )
        == 0
    )
    result = json.loads(out.read_text(encoding="utf-8"))
    assert result["agent_usd"] == pytest.approx(5.0 + 1.0 + 2.5)
    assert result["aisql_usd"] == pytest.approx(8.0)
    assert result["total_usd"] == pytest.approx(16.5)
    assert result["status"] == "exact"
    assert result["price_snapshot"]["retrieved_at"] == "2026-10-29"
