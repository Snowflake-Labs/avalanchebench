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

"""Command-line entry points for setup, scoring, CATC, and monetary cost."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from snowflake.connector.errors import Error as SnowflakeError

from .catc import (
    TokenUsage,
    aggregate_catc,
    load_snapshot,
    measure_component,
    normalize_usage,
)
from .cortex import CortexError, configured_client_factory, configured_models
from .data import DataError, SnowflakeLoader, verify_release_archive
from .evaluation import (
    EvaluationError,
    evaluate,
    load_rubric,
    prompt_template_sha256,
    read_report,
    sha256_bytes,
    write_bytes_atomically,
    write_outputs_atomically,
)
from .evaluation import (
    sha256_file as evaluation_sha256_file,
)
from .monetary import (
    DEFAULT_PRICES,
    aggregate_monetary_cost,
    load_prices,
    measure_cost_component,
)

DATASETS = ("ecommerce", "customer-support", "travel")
DEFAULT_SCHEMAS = {
    "ecommerce": "ECOMMERCE",
    "customer-support": "CUSTOMER_SUPPORT",
    "travel": "TRAVEL",
}


def _repository_root() -> Path:
    configured = os.environ.get("AVBENCH_REPOSITORY_ROOT")
    if configured:
        return Path(configured).resolve()
    candidate = Path.cwd()
    if (candidate / "data/manifest.json").exists() or (candidate / "tasks").is_dir():
        return candidate
    return Path(__file__).resolve().parents[2]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="avbench")
    subparsers = parser.add_subparsers(dest="command", required=True)

    setup = subparsers.add_parser("setup", help="load and validate released data")
    setup.add_argument("--dataset", choices=(*DATASETS, "all"), required=True)
    setup.add_argument("--connection")
    setup.add_argument("--database", default="AVALANCHE_BENCH")
    setup.add_argument("--schema")
    setup.add_argument("--replace", action="store_true")
    setup.add_argument("--data-dir", type=Path)
    setup.add_argument("--state-dir", type=Path, default=Path(".avbench/deployments"))

    score = subparsers.add_parser("score", help="score a saved Markdown report")
    score.add_argument("--task", choices=DATASETS, required=True)
    score.add_argument("--report", type=Path, required=True)
    score.add_argument("--output-dir", type=Path)

    catc = subparsers.add_parser("catc", help="calculate agent plus AISQL CATC")
    catc.add_argument("--agent-usage", type=Path, required=True)
    catc.add_argument("--aisql-usage", type=Path, required=True)
    catc.add_argument("--output", type=Path, required=True)

    cost = subparsers.add_parser(
        "cost", help="calculate agent plus AISQL monetary cost"
    )
    cost.add_argument("--agent-usage", type=Path, required=True)
    cost.add_argument("--aisql-usage", type=Path, required=True)
    cost.add_argument("--prices", type=Path, default=DEFAULT_PRICES)
    cost.add_argument("--output", type=Path, required=True)
    return parser


def _connect(connection_name: str | None) -> Any:
    import snowflake.connector

    if connection_name:
        return snowflake.connector.connect(connection_name=connection_name)
    return snowflake.connector.connect()


def _setup(args: argparse.Namespace) -> int:
    if args.dataset == "all" and args.schema:
        raise DataError("--schema can only be used with one dataset")
    root = _repository_root()
    data_dir = (args.data_dir or root / "data").resolve()
    datasets = DATASETS if args.dataset == "all" else (args.dataset,)
    verified = [
        (dataset, *verify_release_archive(data_dir, dataset)) for dataset in datasets
    ]
    connection = _connect(args.connection)
    try:
        loader = SnowflakeLoader(connection)
        for dataset, archive, _record in verified:
            schema = args.schema or DEFAULT_SCHEMAS[dataset]
            manifest_path, env_path = loader.load(
                archive=archive,
                database=args.database,
                schema=schema,
                replace=args.replace,
                state_dir=args.state_dir,
            )
            print(f"{dataset}: {manifest_path}")
            print(f"{dataset}: source {env_path} before running Harbor")
    finally:
        connection.close()
    return 0


def _score_hashes(root: Path, task: str, report: bytes, rubric: Path) -> dict[str, str]:
    root_manifest = json.loads(
        (root / "data/manifest.json").read_text(encoding="utf-8")
    )
    release_path = root / "tasks" / task / "tests/release.json"
    release = json.loads(release_path.read_text()) if release_path.is_file() else {}
    hashes = {
        "task_instruction_sha256": evaluation_sha256_file(
            root / "tasks" / task / "instruction.md"
        ),
        "dataset_archive_sha256": root_manifest["archives"][task]["sha256"],
        "rubric_sha256": evaluation_sha256_file(rubric),
        "report_sha256": sha256_bytes(report),
        "prompt_template_sha256": prompt_template_sha256(),
        "evaluator_sha256": evaluation_sha256_file(
            root / "src/avalanche_bench/evaluation.py"
        ),
        "extractor_sha256": evaluation_sha256_file(
            root / "src/avalanche_bench/extraction.py"
        ),
        "cortex_transport_sha256": evaluation_sha256_file(
            root / "src/avalanche_bench/cortex.py"
        ),
    }
    if tree := release.get("evaluator_source_tree"):
        hashes["evaluator_source_tree"] = tree
    return hashes


def score_to_paths(
    *,
    task: str,
    report_path: Path,
    reward_path: Path,
    details_path: Path,
    run_path: Path,
    root: Path | None = None,
    client: Any | None = None,
    failure_path: Path | None = None,
    context_mode: str | None = None,
    include_private_prompts: bool = False,
) -> None:
    """Score one report; exposed for the self-contained task verifier."""

    for path in (reward_path, details_path, run_path):
        path.unlink(missing_ok=True)
    if failure_path is not None:
        failure_path.unlink(missing_ok=True)
    root = root or _repository_root()
    rubric_path = root / "tasks" / task / "tests" / "rubric.csv"
    report_bytes = read_report(report_path)
    rubric = load_rubric(rubric_path)
    report = report_bytes.decode("utf-8")
    models = configured_models()
    hashes = _score_hashes(root, task, report_bytes, rubric_path)
    try:
        reward, details, run = evaluate(
            report=report,
            rubric=rubric,
            models=models,
            client=client,
            client_factory=configured_client_factory() if client is None else None,
            task=task,
            hashes=hashes,
            source_tree=hashes.get("evaluator_source_tree"),
            context_mode=context_mode,
            include_private_prompts=include_private_prompts,
        )
    except EvaluationError as exc:
        if failure_path is not None:
            write_outputs_atomically(
                (
                    (
                        failure_path,
                        {
                            "status": "failed_closed",
                            "error_class": type(exc).__name__,
                            "error": str(exc),
                            "audit": exc.audit,
                            "hashes": hashes,
                        },
                    ),
                )
            )
        raise
    write_outputs_atomically(
        (
            (reward_path, reward),
            (details_path, details),
            (run_path, run),
        )
    )


def _score(args: argparse.Namespace) -> int:
    root = _repository_root()
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output_dir = (
        args.output_dir or root / ".avbench/scores" / f"{args.task}-{timestamp}"
    )
    score_to_paths(
        task=args.task,
        report_path=args.report.resolve(),
        reward_path=output_dir / "reward.json",
        details_path=output_dir / "evaluation.json",
        run_path=output_dir / "run.json",
        failure_path=output_dir / "failure.json",
        include_private_prompts=(
            os.environ.get("AVBENCH_INCLUDE_PRIVATE_PROMPTS", "").strip().lower()
            in {"1", "true", "yes"}
        ),
        root=root,
    )
    print(output_dir)
    return 0


def _read_usage(path: Path) -> tuple[list[TokenUsage], str | None]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("usage"), list):
        raise ValueError(f"usage file has no usage list: {path}")
    usages = []
    for item in payload["usage"]:
        if not isinstance(item, dict) or not isinstance(item.get("model"), str):
            raise ValueError(f"invalid usage record in {path}")
        usages.append(normalize_usage(item["model"], item))
    reason = payload.get("partial_reason")
    if reason is not None and not isinstance(reason, str):
        raise ValueError(f"partial_reason must be a string in {path}")
    return usages, reason


def _catc(args: argparse.Namespace) -> int:
    resource = Path(__file__).with_name("resources") / "arena_snapshot.json"
    snapshot = load_snapshot(resource)
    agent_usage, agent_reason = _read_usage(args.agent_usage)
    aisql_usage, aisql_reason = _read_usage(args.aisql_usage)
    result = aggregate_catc(
        measure_component("agent", agent_usage, snapshot, partial_reason=agent_reason),
        measure_component("aisql", aisql_usage, snapshot, partial_reason=aisql_reason),
    )
    result["arena_snapshot"] = snapshot["source"]
    write_bytes_atomically(args.output, (json.dumps(result, indent=2) + "\n").encode())
    print(args.output)
    return 0


def _cost(args: argparse.Namespace) -> int:
    prices = load_prices(args.prices)
    agent_usage, agent_reason = _read_usage(args.agent_usage)
    aisql_usage, aisql_reason = _read_usage(args.aisql_usage)
    result = aggregate_monetary_cost(
        measure_cost_component(
            "agent", agent_usage, prices, partial_reason=agent_reason
        ),
        measure_cost_component(
            "aisql", aisql_usage, prices, partial_reason=aisql_reason
        ),
    )
    result["price_snapshot"] = prices["source"]
    write_bytes_atomically(args.output, (json.dumps(result, indent=2) + "\n").encode())
    print(args.output)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "setup":
            return _setup(args)
        if args.command == "score":
            return _score(args)
        if args.command == "catc":
            return _catc(args)
        if args.command == "cost":
            return _cost(args)
        raise AssertionError(args.command)
    except (
        CortexError,
        DataError,
        EvaluationError,
        SnowflakeError,
        ValueError,
        OSError,
    ) as exc:
        print(f"avbench: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
