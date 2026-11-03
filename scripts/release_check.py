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

"""Fail-closed structural and artifact checks for AvalancheBench releases."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from release_assets import document_content

from avalanche_bench.data import extract_and_verify_archive, is_lfs_pointer, sha256_file

ROOT = Path(__file__).resolve().parents[1]
TASKS = ("ecommerce", "customer-support", "travel")
FORBIDDEN_PATH_PARTS = {
    "jobs",
    "trials",
    "private-data",
    "private-results",
    "outputs",
    "trajectories",
    "cache",
    "hidden",
    "generators",
}
FORBIDDEN_PATH_FRAGMENTS = {
    "answer_cache",
    "answer_key",
    "generator_prompt",
    "generation_prompt",
    "latent_world",
    "oracle_",
    "trajectory",
}
FORBIDDEN_PROMPT = re.compile(
    r"(?:TEMP\.|AVALANCHE_(?:ECOMMERCE|CUSTOMER_SUPPORT|TRAVEL)|"
    + r"Snow"
    + r"house|"
    + r"Snowflake-only|[a-z0-9_-]+-snowflake-v[0-9])",
    re.IGNORECASE,
)
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
FORBIDDEN_ARCHIVE_TEXT = (
    b"snow" + b"house",
    b"snowflakecomputing.com",
    b"temp.avalanche_",
    b"/home/",
    b"c:\\users\\",
    b"generator_prompt",
    b"oracle_answer",
    b"private_key",
    b"api_key",
    b"password=",
    b"-----begin private key-----",
    b"cresta",
    b"nordform",
)
FORBIDDEN_HISTORY_TEXT = {
    "employee filesystem path": re.compile(
        rb"(?:/ho" + rb"me/|/Users/|[a-z]:\\Users\\)", re.IGNORECASE
    ),
    "internal Snowflake account URL": re.compile(
        rb"https?://(?!(?:<account_identifier>|example)\.)"
        + rb"[a-z0-9._-]+\."
        + rb"snowflake"
        + rb"computing\.com",
        re.IGNORECASE,
    ),
    "internal task source name": re.compile(
        rb"[a-z0-9_-]+-snowflake-v[0-9]",
        re.IGNORECASE,
    ),
    "private Snowflake alias": re.compile(rb"snow" + rb"house", re.IGNORECASE),
    "private Snowflake object": re.compile(rb"temp\." + rb"avalanche_", re.IGNORECASE),
}


def fail(message: str) -> None:
    raise RuntimeError(message)


def tracked_files() -> list[Path]:
    output = (
        subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
        .decode()
        .split("\0")
    )
    return [Path(item) for item in output if item]


def is_forbidden_path(path: Path) -> bool:
    lowered = path.as_posix().lower()
    lowered_parts = {part.lower() for part in path.parts}
    return bool(FORBIDDEN_PATH_PARTS.intersection(lowered_parts)) or any(
        fragment in lowered for fragment in FORBIDDEN_PATH_FRAGMENTS
    )


def check_paths(paths: list[Path]) -> None:
    for path in paths:
        if is_forbidden_path(path):
            fail(f"forbidden tracked path: {path}")
        absolute = ROOT / path
        if absolute.is_symlink():
            target = absolute.resolve()
            if not target.is_relative_to(ROOT):
                fail(f"tracked symlink escapes repository: {path}")
    task_directories = sorted(path.name for path in (ROOT / "tasks").iterdir())
    if task_directories != sorted(TASKS):
        fail(f"expected exactly three clean task directories, got {task_directories}")


def check_task(task: str, runtime_requirements: bytes) -> None:
    root = ROOT / "tasks" / task
    required = (
        "instruction.md",
        "task.toml",
        "environment/Dockerfile",
        "environment/docker-compose.yaml",
        "environment/requirements.lock",
        "tests/test.sh",
        "tests/rubric.csv",
        "tests/release.json",
    )
    for relative in required:
        if not (root / relative).is_file():
            fail(f"{task} is missing {relative}")
    package_files = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.suffix != ".pyc"
    )
    expected_package_files = [
        "environment/Dockerfile",
        "environment/docker-compose.yaml",
        "environment/requirements.lock",
        "environment/runtime/avalanche_bench/__init__.py",
        "environment/runtime/avalanche_bench/manifests.py",
        "environment/runtime/avalanche_bench/materialize_runtime.py",
        "instruction.md",
        "task.toml",
        "tests/avalanche_bench/__init__.py",
        "tests/avalanche_bench/cortex.py",
        "tests/avalanche_bench/evaluation.py",
        "tests/avalanche_bench/extraction.py",
        "tests/avalanche_bench/task_verifier.py",
        "tests/release.json",
        "tests/release.json.license",
        "tests/rubric.csv",
        "tests/rubric.csv.license",
        "tests/test.sh",
    ]
    if package_files != expected_package_files:
        fail(f"{task} has an unexpected publishable file list: {package_files}")
    runtime_files = sorted(
        path.relative_to(root).as_posix()
        for path in (root / "environment/runtime").rglob("*")
        if path.is_file()
    )
    expected_runtime_files = [
        "environment/runtime/avalanche_bench/__init__.py",
        "environment/runtime/avalanche_bench/manifests.py",
        "environment/runtime/avalanche_bench/materialize_runtime.py",
    ]
    if runtime_files != expected_runtime_files:
        fail(f"{task} has unexpected container runtime files: {runtime_files}")
    requirements = (root / "environment/requirements.lock").read_bytes()
    # License comments do not change the pinned dependency specifications.
    requirement_lines = [
        line.strip()
        for line in requirements.splitlines()
        if line.strip() and not line.lstrip().startswith(b"#")
    ]
    runtime_lines = [
        line.strip()
        for line in runtime_requirements.splitlines()
        if line.strip() and not line.lstrip().startswith(b"#")
    ]
    if requirement_lines != runtime_lines:
        fail(f"{task} container requirements have drifted from uv.lock")
    prompt = (root / "instruction.md").read_text(encoding="utf-8")
    if FORBIDDEN_PROMPT.search(prompt):
        fail(f"{task} instruction contains a private physical name")
    release = json.loads((root / "tests/release.json").read_text(encoding="utf-8"))
    expected = {
        "instruction_sha256": sha256_file(root / "instruction.md"),
        "rubric_sha256": sha256_file(root / "tests/rubric.csv"),
        "evaluator_sha256": sha256_file(ROOT / "src/avalanche_bench/evaluation.py"),
        "extractor_sha256": sha256_file(ROOT / "src/avalanche_bench/extraction.py"),
        "cortex_sha256": sha256_file(ROOT / "src/avalanche_bench/cortex.py"),
    }
    for key, value in expected.items():
        if release.get(key) != value:
            fail(f"{task} {key} is stale")
    source_tree = release.get("evaluator_source_tree")
    if source_tree is not None:
        if not isinstance(source_tree, str) or not re.fullmatch(
            r"[0-9a-f]{40}", source_tree
        ):
            fail(f"{task} evaluator_source_tree is not a full Git tree identifier")
        actual_tree = (
            subprocess.check_output(
                ["git", "rev-parse", "HEAD:src/avalanche_bench"], cwd=ROOT
            )
            .decode()
            .strip()
        )
        if source_tree != actual_tree:
            fail(f"{task} evaluator source tree differs from the release source")
        reference = source_tree
    else:
        # Older private development snapshots used commit provenance.
        source_commit = str(release.get("evaluator_source_commit", ""))
        if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
            fail(f"{task} evaluator_source_commit is not a full Git commit")
        reference = (
            subprocess.check_output(
                [
                    "git",
                    "rev-parse",
                    "--verify",
                    f"{source_commit}:src/avalanche_bench",
                ],
                cwd=ROOT,
            )
            .decode()
            .strip()
        )
    for name, field in (
        ("evaluation.py", "evaluator_sha256"),
        ("extraction.py", "extractor_sha256"),
        ("cortex.py", "cortex_sha256"),
    ):
        source = subprocess.check_output(
            ["git", "show", f"{reference}:{name}"], cwd=ROOT
        )
        if hashlib.sha256(source).hexdigest() != release[field]:
            fail(f"{task} {name} source provenance does not match its content hash")
    dockerfile = (root / "environment/Dockerfile").read_text(encoding="utf-8")
    instructions = [
        line
        for line in dockerfile.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    first = instructions[0]
    if not re.fullmatch(r"FROM [^\s]+@sha256:[0-9a-f]{64}", first):
        fail(f"{task} base image is not digest-pinned")
    expected_instructions = [
        first,
        "COPY requirements.lock /opt/avbench/requirements.lock",
        "RUN pip install --no-cache-dir --upgrade pip==26.2.2 && "
        "pip install --no-cache-dir --no-deps -r "
        "/opt/avbench/requirements.lock && pip check",
        "COPY runtime /opt/avbench",
        "WORKDIR /app",
    ]
    if instructions != expected_instructions:
        fail(f"{task} Dockerfile has unexpected build instructions")


def check_dataset_manifest() -> None:
    payload = tomllib.loads((ROOT / "dataset.toml").read_text(encoding="utf-8"))
    if payload["dataset"].get("version") != "0.1.0":
        fail("dataset version is not 0.1.0")
    tasks = payload.get("tasks", [])
    expected_names = {f"snowflake-labs/avalanchebench-{task}" for task in TASKS}
    if len(tasks) != 3 or {item.get("name") for item in tasks} != expected_names:
        fail("dataset.toml must contain exactly the three public tasks")
    if any(not DIGEST.fullmatch(str(item.get("digest", ""))) for item in tasks):
        fail("dataset.toml contains an invalid task digest")
    by_name = {item["name"]: item["digest"] for item in tasks}
    for task in TASKS:
        task_root = ROOT / "tasks" / task
        package_files = [task_root / "task.toml", task_root / "instruction.md"]
        package_files.extend(
            path
            for directory in (task_root / "environment", task_root / "tests")
            for path in directory.rglob("*")
            if path.is_file() and path.suffix != ".pyc"
        )
        outer = hashlib.sha256()
        for path in sorted(
            package_files, key=lambda item: item.relative_to(task_root).as_posix()
        ):
            relative = path.relative_to(task_root).as_posix()
            outer.update(f"{relative}\0{sha256_file(path)}\n".encode())
        expected = by_name[f"snowflake-labs/avalanchebench-{task}"]
        if expected != f"sha256:{outer.hexdigest()}":
            fail(f"dataset.toml digest is stale for {task}")


def check_data(*, full: bool) -> None:
    root = json.loads((ROOT / "data/manifest.json").read_text(encoding="utf-8"))
    card = (ROOT / "docs/dataset-card.md").read_text(encoding="utf-8")
    if set(root.get("archives", {})) != set(TASKS):
        fail("data/manifest.json must contain exactly three archives")
    for dataset in TASKS:
        record = root["archives"][dataset]
        if record["sha256"] not in card:
            fail(f"{dataset} dataset-card checksum is stale")
        archive = ROOT / "data" / record["path"]
        release = json.loads(
            (ROOT / "tasks" / dataset / "tests/release.json").read_text(
                encoding="utf-8"
            )
        )
        if release["dataset_archive_sha256"] != record["sha256"]:
            fail(f"{dataset} task/archive hash is stale")
        if full:
            if is_lfs_pointer(archive):
                fail(f"{archive} is not materialized")
            if archive.stat().st_size != record["bytes"]:
                fail(f"{dataset} outer size mismatch")
            if sha256_file(archive) != record["sha256"]:
                fail(f"{dataset} outer hash mismatch")
            with tempfile.TemporaryDirectory(prefix="avbench-release-check-") as tmp:
                extracted = Path(tmp)
                extract_and_verify_archive(archive, extracted)
                scan_extracted_archive(extracted)
        else:
            staged = subprocess.check_output(
                ["git", "show", f":data/{record['path']}"], cwd=ROOT
            )
            if not staged.startswith(b"version https://git-lfs.github.com/spec/v1\n"):
                fail(f"data/{record['path']} is not staged as an LFS pointer")


def _scan_bytes(content: bytes, label: str) -> None:
    lowered = content.lower()
    for forbidden in FORBIDDEN_ARCHIVE_TEXT:
        if forbidden in lowered:
            fail(f"forbidden release text {forbidden!r} in {label}")


def scan_extracted_archive(root: Path) -> None:
    forbidden_names = {
        ".env",
        "generator_prompt",
        "generation_prompt",
        "answer_key",
        "oracle_",
        "trajectory",
        "latent_world",
    }
    documents = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        lowered_name = path.name.lower()
        if any(part in lowered_name for part in forbidden_names):
            fail(f"forbidden release filename: {path.relative_to(root)}")
        content = path.read_bytes()
        _scan_bytes(content, str(path.relative_to(root)))
        if path.suffix.lower() in {".pdf", ".docx", ".pptx", ".jpg", ".jpeg"}:
            documents.append(path)
        elif zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as nested:
                for name in nested.namelist():
                    if name.endswith("/"):
                        continue
                    _scan_bytes(nested.read(name), f"{path.relative_to(root)}!{name}")
    with ProcessPoolExecutor(max_workers=6) as pool:
        for index, (path, contents) in enumerate(
            zip(documents, pool.map(document_content, documents), strict=True), 1
        ):
            for content in contents:
                _scan_bytes(content, str(path.relative_to(root)))
            if index % 100 == 0:
                print(f"scanned {index}/{len(documents)} assets", flush=True)
    print(f"scanned {len(documents)} documents/images, including OCR", flush=True)


def check_history_paths() -> None:
    output = subprocess.check_output(
        ["git", "log", "--all", "--format=", "--name-only"], cwd=ROOT
    ).decode(errors="replace")
    for raw in output.splitlines():
        path = Path(raw.strip())
        if raw.strip() and is_forbidden_path(path):
            fail(f"forbidden path appears in Git history: {path}")


def check_history_contents() -> None:
    commits = subprocess.check_output(
        ["git", "rev-list", "--all"], cwd=ROOT
    ).splitlines()
    seen: set[tuple[bytes, str]] = set()
    for commit in commits:
        tree = subprocess.check_output(["git", "ls-tree", "-r", "-z", commit], cwd=ROOT)
        for entry in tree.split(b"\0"):
            if not entry:
                continue
            metadata, raw_path = entry.split(b"\t", 1)
            _mode, kind, object_id = metadata.split(b" ", 2)
            if kind != b"blob":
                continue
            path = raw_path.decode("utf-8", errors="surrogateescape")
            key = (object_id, path)
            if key in seen or path == "scripts/release_check.py":
                continue
            seen.add(key)
            content = subprocess.check_output(
                ["git", "cat-file", "blob", object_id], cwd=ROOT
            )
            for label, pattern in FORBIDDEN_HISTORY_TEXT.items():
                if pattern.search(content):
                    fail(f"{label} appears in Git history: {object_id.decode()}:{path}")


def check_runtime_sync() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/sync_task_runtime.py"), "--check"],
        cwd=ROOT,
        check=False,
    )
    if result.returncode:
        fail("vendored task runtime is stale")


def exported_runtime_requirements() -> bytes:
    return subprocess.check_output(
        [
            "uv",
            "export",
            "--frozen",
            "--no-dev",
            "--no-emit-project",
            "--format",
            "requirements-txt",
            "--no-hashes",
            "--no-annotate",
            "--no-header",
        ],
        cwd=ROOT,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()
    paths = tracked_files()
    check_paths(paths)
    check_dataset_manifest()
    check_runtime_sync()
    runtime_requirements = exported_runtime_requirements()
    for task in TASKS:
        check_task(task, runtime_requirements)
    check_data(full=args.full)
    if args.full:
        check_history_paths()
        check_history_contents()
    print("release checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
