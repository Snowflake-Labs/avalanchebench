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

"""Create a local publication candidate without development history or tags.

Copies only Git-tracked working-tree files. Stage new reviewed files first.
No remotes, pushes, registry publication, or visibility changes are performed.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

from avalanche_bench.data import is_lfs_pointer

ROOT = Path(__file__).resolve().parents[1]


def git(destination: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Snowflake Inc.",
            "-c",
            "user.email=opensource@snowflake.com",
            *args,
        ],
        cwd=destination,
        check=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    destination = args.destination.resolve()
    if destination.exists() or destination.is_relative_to(ROOT):
        parser.error("destination must be a new directory outside this repository")
    untracked = subprocess.check_output(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=ROOT
    )
    if untracked.strip(b"\0"):
        parser.error(
            "stage or remove untracked files before exporting the reviewed tree"
        )
    paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).split(b"\0")
    destination.mkdir(parents=True)
    for raw in paths:
        if not raw:
            continue
        path = Path(raw.decode())
        if path.parts[0] in {".avbench", ".git", ".venv", "jobs", "trials"}:
            raise RuntimeError(f"private path is tracked: {path}")
        if path.name == ".env" or (ROOT / path).is_symlink():
            raise RuntimeError(f"unsafe snapshot path: {path}")
        if path.suffix == ".zip" and is_lfs_pointer(ROOT / path):
            raise RuntimeError(f"materialize archive before exporting: {path}")
        target = destination / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / path, target)
    # The new source snapshot must not retain development commit references.
    for release_path in (destination / "tasks").glob("*/tests/release.json"):
        release = json.loads(release_path.read_text())
        release.pop("evaluator_source_commit", None)
        release_path.write_text(json.dumps(release, indent=2, sort_keys=True) + "\n")
    git(destination, "init", "--initial-branch=main")
    git(destination, "lfs", "install", "--local")
    git(destination, "add", ".")
    subprocess.run(
        ["uv", "run", "scripts/refresh_release_metadata.py", "--staged"],
        cwd=destination,
        check=True,
    )
    git(destination, "add", "tasks", "dataset.toml")
    git(destination, "commit", "-m", "Import reviewed AvalancheBench release snapshot")
    count = subprocess.check_output(
        ["git", "rev-list", "--count", "--all"], cwd=destination
    ).strip()
    if count != b"1":
        raise RuntimeError("publication snapshot must contain exactly one commit")
    subprocess.run(
        ["uv", "run", "scripts/release_check.py", "--full"], cwd=destination, check=True
    )
    print(f"Verified local release candidate: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
