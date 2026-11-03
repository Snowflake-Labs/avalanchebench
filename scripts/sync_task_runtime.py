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

"""Synchronize canonical runtime modules into self-contained Harbor tasks."""

from __future__ import annotations

import argparse
import filecmp
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src/avalanche_bench"
TASKS = ("ecommerce", "customer-support", "travel")
VERIFIER_FILES = (
    "__init__.py",
    "cortex.py",
    "evaluation.py",
    "extraction.py",
    "task_verifier.py",
)
ENVIRONMENT_FILES = (
    "__init__.py",
    "manifests.py",
    "materialize_runtime.py",
)


def destinations(task: str) -> tuple[tuple[Path, tuple[str, ...]], ...]:
    task_root = ROOT / "tasks" / task
    return (
        (task_root / "tests/avalanche_bench", VERIFIER_FILES),
        (task_root / "environment/runtime/avalanche_bench", ENVIRONMENT_FILES),
    )


def synchronized(task: str) -> bool:
    return all(
        (destination / name).is_file()
        and filecmp.cmp(SOURCE / name, destination / name, shallow=False)
        for destination, names in destinations(task)
        for name in names
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        drifted = [task for task in TASKS if not synchronized(task)]
        if drifted:
            parser.error("runtime drift: " + ", ".join(drifted))
        return 0
    for task in TASKS:
        for destination, names in destinations(task):
            destination.mkdir(parents=True, exist_ok=True)
            for name in names:
                shutil.copyfile(SOURCE / name, destination / name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
