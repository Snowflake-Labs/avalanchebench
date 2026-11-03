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

"""Capability-adjusted token cost (CATC) for agent and AISQL usage only."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

Status = Literal["exact", "estimated", "partial_known_lower_bound", "unavailable"]


@dataclass(frozen=True)
class TokenUsage:
    model_name: str
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    estimated: bool = False

    def __post_init__(self) -> None:
        if any(value < 0 for value in asdict(self).values() if isinstance(value, int)):
            raise ValueError("token counts cannot be negative")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
            + self.output_tokens
        )


@dataclass(frozen=True)
class CatcComponent:
    component: Literal["agent", "aisql"]
    status: Status
    catc: float | None
    known_catc_lower_bound: float
    usage: tuple[TokenUsage, ...]
    reason: str | None = None

    @property
    def total_tokens(self) -> int:
        return sum(item.total_tokens for item in self.usage)

    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "status": self.status,
            "catc": self.catc,
            "total_tokens": self.total_tokens,
            "known_catc_lower_bound": self.known_catc_lower_bound,
            "usage": [item.to_dict() for item in self.usage],
            "reason": self.reason,
        }


def load_snapshot(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "source",
        "anchor_model",
        "capability_weights",
        "cortex_to_arena_model_name",
        "ratings",
    }
    if not isinstance(payload, dict) or not required.issubset(payload):
        raise ValueError("Arena snapshot is incomplete")
    return payload


def capability_factor(model_name: str, snapshot: dict[str, Any]) -> float:
    try:
        arena_name = snapshot["cortex_to_arena_model_name"][model_name]
        anchor = snapshot["anchor_model"]
        model_rating = float(snapshot["ratings"][arena_name]["rating"])
        anchor_rating = float(snapshot["ratings"][anchor]["rating"])
    except KeyError as exc:
        raise ValueError(
            f"Arena mapping/rating unavailable for {model_name!r}"
        ) from exc
    return 10 ** ((model_rating - anchor_rating) / 400)


def weighted_tokens(usage: TokenUsage) -> float:
    return (
        1.0 * usage.input_tokens
        + 0.10 * usage.cache_read_tokens
        + 1.25 * usage.cache_write_tokens
        + 5.00 * usage.output_tokens
    )


def usage_catc(usage: TokenUsage, snapshot: dict[str, Any]) -> float:
    return capability_factor(usage.model_name, snapshot) * weighted_tokens(usage)


def normalize_usage(model_name: str, raw: dict[str, Any]) -> TokenUsage:
    def first(*names: str) -> int:
        for name in names:
            value = raw.get(name)
            if value is not None:
                return int(value)
        return 0

    cache_read = first(
        "cache_read_input_tokens",
        "cached_input_tokens",
        "cache_read_tokens",
        "cache_input_tokens",
        "cache_input",
    )
    cache_write = first(
        "cache_creation_input_tokens",
        "cache_write_input_tokens",
        "cache_write_tokens",
        "cache_write",
    )
    if raw.get("uncached_input_tokens") is not None:
        input_tokens = int(raw["uncached_input_tokens"])
    elif raw.get("input_tokens") is not None:
        input_tokens = int(raw["input_tokens"])
    elif raw.get("input") is not None:
        input_tokens = int(raw["input"])
    else:
        input_tokens = max(
            first("prompt_tokens") - cache_read - cache_write,
            0,
        )
    return TokenUsage(
        model_name=model_name,
        input_tokens=input_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        output_tokens=first("output_tokens", "completion_tokens", "output"),
        estimated=raw.get("estimated") is True,
    )


def measure_component(
    component: Literal["agent", "aisql"],
    usages: list[TokenUsage],
    snapshot: dict[str, Any],
    *,
    partial_reason: str | None = None,
) -> CatcComponent:
    measured = 0.0
    estimated = 0.0
    unknown: list[str] = []
    for usage in usages:
        try:
            value = usage_catc(usage, snapshot)
            if usage.estimated:
                estimated += value
            else:
                measured += value
        except ValueError:
            unknown.append(usage.model_name)
    total = measured + estimated
    if unknown:
        reason = "Arena mapping unavailable for: " + ", ".join(sorted(set(unknown)))
        if partial_reason:
            reason += f"; {partial_reason}"
        return CatcComponent(
            component=component,
            status="partial_known_lower_bound" if measured else "unavailable",
            catc=None,
            known_catc_lower_bound=measured,
            usage=tuple(usages),
            reason=reason,
        )
    if partial_reason:
        return CatcComponent(
            component=component,
            status="partial_known_lower_bound" if measured else "unavailable",
            catc=None,
            known_catc_lower_bound=measured,
            usage=tuple(usages),
            reason=partial_reason,
        )
    if estimated:
        return CatcComponent(
            component=component,
            status="estimated",
            catc=total,
            known_catc_lower_bound=measured,
            usage=tuple(usages),
            reason="includes estimated token attribution",
        )
    return CatcComponent(
        component=component,
        status="exact",
        catc=measured,
        known_catc_lower_bound=measured,
        usage=tuple(usages),
    )


def aggregate_catc(agent: CatcComponent, aisql: CatcComponent) -> dict[str, Any]:
    if agent.component != "agent" or aisql.component != "aisql":
        raise ValueError("total_catc accepts only agent and agent-issued AISQL")
    complete = {agent.status, aisql.status} <= {"exact", "estimated"}
    exact = agent.status == aisql.status == "exact"
    lower_bound = agent.known_catc_lower_bound + aisql.known_catc_lower_bound
    return {
        "agent_catc": agent.catc,
        "aisql_catc": aisql.catc,
        "total_catc": ((agent.catc or 0.0) + (aisql.catc or 0.0) if complete else None),
        "agent_total_tokens": agent.total_tokens
        if agent.status in {"exact", "estimated"}
        else None,
        "aisql_total_tokens": aisql.total_tokens
        if aisql.status in {"exact", "estimated"}
        else None,
        "total_tokens": agent.total_tokens + aisql.total_tokens if complete else None,
        "known_catc_lower_bound": lower_bound,
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
