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

"""Monetary cost of agent and AISQL usage at pinned third-party list prices."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .catc import Status, TokenUsage

TOKEN_TYPES = ("input", "cache_read", "cache_write", "output")
DEFAULT_PRICES = Path(__file__).with_name("resources") / "provider_prices.json"
PRICE_UNIT = "USD per million tokens"


@dataclass(frozen=True)
class CostComponent:
    component: Literal["agent", "aisql"]
    status: Status
    usd: float | None
    known_usd_lower_bound: float
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "status": self.status,
            "usd": self.usd,
            "known_usd_lower_bound": self.known_usd_lower_bound,
            "reason": self.reason,
        }


def load_prices(path: Path = DEFAULT_PRICES) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not {"source", "unit", "models"} <= set(
        payload
    ):
        raise ValueError("price snapshot is incomplete")
    if payload["unit"] != PRICE_UNIT:
        raise ValueError(f"price snapshot unit must be {PRICE_UNIT!r}")
    models = payload["models"]
    if not isinstance(models, dict):
        raise ValueError("price snapshot models must be a JSON object")
    for model, price in models.items():
        if not isinstance(price, dict) or not set(TOKEN_TYPES) <= set(price):
            raise ValueError(f"invalid price for {model!r}")
        for token_type in TOKEN_TYPES:
            rate = price[token_type]
            if rate is None:
                continue
            if isinstance(rate, bool) or not isinstance(rate, (int, float)):
                raise ValueError(f"invalid {token_type} price for {model!r}")
            if rate < 0:
                raise ValueError(f"negative {token_type} price for {model!r}")
    return payload


def usage_cost(usage: TokenUsage, prices: dict[str, Any]) -> float:
    price = prices["models"].get(usage.model_name)
    if price is None:
        raise ValueError(f"price unavailable for {usage.model_name!r}")
    tokens = (
        usage.input_tokens,
        usage.cache_read_tokens,
        usage.cache_write_tokens,
        usage.output_tokens,
    )
    total = 0.0
    for token_type, count in zip(TOKEN_TYPES, tokens, strict=True):
        if not count:
            continue
        rate = price[token_type]
        if rate is None:
            raise ValueError(f"{token_type} price unavailable for {usage.model_name!r}")
        total += rate * count / 1_000_000
    return total


def measure_cost_component(
    component: Literal["agent", "aisql"],
    usages: list[TokenUsage],
    prices: dict[str, Any],
    *,
    partial_reason: str | None = None,
) -> CostComponent:
    measured = 0.0
    estimated = 0.0
    unpriced: list[str] = []
    for usage in usages:
        try:
            value = usage_cost(usage, prices)
        except ValueError as exc:
            unpriced.append(str(exc))
            continue
        if usage.estimated:
            estimated += value
        else:
            measured += value
    reasons = sorted(set(unpriced))
    if partial_reason:
        reasons.append(partial_reason)
    if reasons:
        return CostComponent(
            component=component,
            status="partial_known_lower_bound" if measured else "unavailable",
            usd=None,
            known_usd_lower_bound=measured,
            reason="; ".join(reasons),
        )
    if estimated:
        return CostComponent(
            component=component,
            status="estimated",
            usd=measured + estimated,
            known_usd_lower_bound=measured,
            reason="includes estimated token attribution",
        )
    return CostComponent(
        component=component,
        status="exact",
        usd=measured,
        known_usd_lower_bound=measured,
    )


def aggregate_monetary_cost(
    agent: CostComponent, aisql: CostComponent
) -> dict[str, Any]:
    if agent.component != "agent" or aisql.component != "aisql":
        raise ValueError("total_usd accepts only agent and agent-issued AISQL")
    complete = {agent.status, aisql.status} <= {"exact", "estimated"}
    exact = agent.status == aisql.status == "exact"
    lower_bound = agent.known_usd_lower_bound + aisql.known_usd_lower_bound
    return {
        "agent_usd": agent.usd,
        "aisql_usd": aisql.usd,
        "total_usd": (agent.usd or 0.0) + (aisql.usd or 0.0) if complete else None,
        "known_usd_lower_bound": lower_bound,
        "status": (
            "exact"
            if exact
            else (
                "estimated"
                if complete
                else ("partial_known_lower_bound" if lower_bound else "unavailable")
            )
        ),
        "components": [agent.to_dict(), aisql.to_dict()],
        "judge_calls_included": False,
    }
