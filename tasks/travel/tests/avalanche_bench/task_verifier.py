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

"""Self-contained Harbor verifier entry point vendored into each task."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .cortex import configured_client_factory, configured_models
from .evaluation import (
    EvaluationError,
    evaluate,
    load_rubric,
    prompt_template_sha256,
    read_report,
    sha256_bytes,
    sha256_file,
    write_bytes_atomically,
    write_outputs_atomically,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True)
    parser.add_argument("--report", type=Path, default=Path("/app/report.md"))
    parser.add_argument("--rubric", type=Path, default=Path("/tests/rubric.csv"))
    parser.add_argument("--release", type=Path, default=Path("/tests/release.json"))
    parser.add_argument(
        "--reward-output", type=Path, default=Path("/logs/verifier/reward.json")
    )
    parser.add_argument(
        "--details-output",
        type=Path,
        default=Path("/logs/artifacts/evaluation.json"),
    )
    parser.add_argument(
        "--run-output", type=Path, default=Path("/logs/artifacts/run.json")
    )
    parser.add_argument("--failure-output", type=Path)
    parser.add_argument("--report-artifact", type=Path)
    args = parser.parse_args()

    args.reward_output.unlink(missing_ok=True)
    args.details_output.unlink(missing_ok=True)
    args.run_output.unlink(missing_ok=True)
    if args.failure_output is not None:
        args.failure_output.unlink(missing_ok=True)
    if args.report_artifact is not None:
        args.report_artifact.unlink(missing_ok=True)
    report_bytes = read_report(args.report)
    report = report_bytes.decode("utf-8")
    if args.report_artifact is not None:
        write_bytes_atomically(args.report_artifact, report_bytes)
    rubric = load_rubric(args.rubric)
    release = json.loads(args.release.read_text(encoding="utf-8"))
    hashes = {
        "task_instruction_sha256": release["instruction_sha256"],
        "dataset_archive_sha256": release["dataset_archive_sha256"],
        "cortex_transport_sha256": release["cortex_sha256"],
        "rubric_sha256": sha256_file(args.rubric),
        "report_sha256": sha256_bytes(report_bytes),
        "prompt_template_sha256": prompt_template_sha256(),
        "evaluator_sha256": release["evaluator_sha256"],
        "extractor_sha256": release["extractor_sha256"],
    }
    try:
        reward, details, run = evaluate(
            report=report,
            rubric=rubric,
            models=configured_models(),
            client_factory=configured_client_factory(),
            task=args.task,
            hashes=hashes,
            source_commit=release.get("evaluator_source_commit"),
            source_tree=release.get("evaluator_source_tree"),
            include_private_prompts=(
                os.environ.get("AVBENCH_INCLUDE_PRIVATE_PROMPTS", "").strip().lower()
                in {"1", "true", "yes"}
            ),
        )
    except EvaluationError as exc:
        if args.failure_output is not None:
            write_outputs_atomically(
                (
                    (
                        args.failure_output,
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
            (args.details_output, details),
            (args.run_output, run),
        )
    )
    # Harbor reads this root-owned bind mount before Docker repairs ownership.
    # Publish only numeric rewards with host-readable permissions, and publish last.
    write_outputs_atomically(((args.reward_output, reward),), mode=0o644)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
