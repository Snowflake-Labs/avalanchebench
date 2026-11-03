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

"""Refresh task provenance and Harbor digests against a real source tree."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

from avalanche_bench.data import sha256_file

ROOT = Path(__file__).resolve().parents[1]
TASKS = ("ecommerce", "customer-support", "travel")
SOURCE_FILES = {
    "evaluator_sha256": "src/avalanche_bench/evaluation.py",
    "extractor_sha256": "src/avalanche_bench/extraction.py",
    "cortex_sha256": "src/avalanche_bench/cortex.py",
}


def task_digest(root: Path) -> str:
    files = [root / "task.toml", root / "instruction.md"]
    files.extend(
        path
        for directory in (root / "environment", root / "tests")
        for path in directory.rglob("*")
        if path.is_file() and path.suffix != ".pyc"
    )
    digest = hashlib.sha256()
    for path in sorted(files, key=lambda p: p.relative_to(root).as_posix()):
        digest.update(
            f"{path.relative_to(root).as_posix()}\0{sha256_file(path)}\n".encode()
        )
    return f"sha256:{digest.hexdigest()}"


def refresh(source_commit: str = "HEAD", *, source_tree: str | None = None) -> None:
    reference = source_tree or f"{source_commit}:src/avalanche_bench"
    resolved = (
        subprocess.check_output(["git", "rev-parse", "--verify", reference], cwd=ROOT)
        .decode()
        .strip()
    )
    tree = (
        subprocess.check_output(
            ["git", "rev-parse", "--verify", f"{resolved}^{{tree}}"], cwd=ROOT
        )
        .decode()
        .strip()
    )
    hashes = {}
    for key, relative in SOURCE_FILES.items():
        committed = subprocess.check_output(
            ["git", "show", f"{tree}:{Path(relative).name}"], cwd=ROOT
        )
        current = (ROOT / relative).read_bytes()
        if committed != current:
            raise RuntimeError(f"tree source differs from working tree: {relative}")
        hashes[key] = hashlib.sha256(current).hexdigest()
    archives = json.loads((ROOT / "data/manifest.json").read_text())["archives"]
    dataset_path = ROOT / "dataset.toml"
    dataset = dataset_path.read_text()
    for task in TASKS:
        root = ROOT / "tasks" / task
        path = root / "tests/release.json"
        release = json.loads(path.read_text())
        release.pop("evaluator_source_commit", None)
        release.update(hashes)
        release.update(
            evaluator_source_tree=tree,
            instruction_sha256=sha256_file(root / "instruction.md"),
            rubric_sha256=sha256_file(root / "tests/rubric.csv"),
            dataset_archive_sha256=archives[task]["sha256"],
        )
        path.write_text(json.dumps(release, indent=2, sort_keys=True) + "\n")
        pattern = (
            rf'(name = "snowflake-labs/avalanchebench-{re.escape(task)}"\s*'
            r'digest = ")[^"]+'
        )
        # Preserve the manifest's formatting; replace exactly one task digest.
        dataset, count = re.subn(
            pattern, lambda m, root=root: m.group(1) + task_digest(root), dataset
        )
        if count != 1:
            raise RuntimeError(f"dataset manifest must contain exactly one {task}")
    dataset_path.write_text(dataset)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--source-commit", default="HEAD")
    source.add_argument("--source-tree")
    source.add_argument("--staged", action="store_true")
    args = parser.parse_args()
    tree = args.source_tree
    if args.staged:
        staged = (
            subprocess.check_output(["git", "write-tree"], cwd=ROOT).decode().strip()
        )
        tree = f"{staged}:src/avalanche_bench"
    refresh(args.source_commit, source_tree=tree)
    print("refreshed source hashes, archive hashes, and Harbor task digests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
