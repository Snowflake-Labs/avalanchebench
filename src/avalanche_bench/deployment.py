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

"""Creation of backend-neutral deployment manifests."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from .manifests import (
    BENCHMARK_VERSION,
    REQUIRED_OBJECTS,
    SCHEMA_VERSION,
    canonical_json,
    encode_deployment_manifest,
    validate_deployment_manifest,
)

DEFAULT_STATE_DIR = Path(".avbench/deployments")


def snowflake_manifest(dataset: str, *, database: str, schema: str) -> dict[str, Any]:
    """Build the manifest for objects created by the reference loader."""

    prefix = f"{database}.{schema}"
    objects: dict[str, dict[str, str]] = {}
    for logical_name, kind in REQUIRED_OBJECTS[dataset].items():
        physical_name = logical_name.upper()
        if kind == "document_collection":
            physical_name = "KNOWLEDGE_BASE"
        elif kind == "asset_collection":
            physical_name = "IMAGES"
        identifier = (
            f"@{prefix}.{physical_name}"
            if kind != "table"
            else (f"{prefix}.{physical_name}")
        )
        objects[logical_name] = {"kind": kind, "identifier": identifier}
    return validate_deployment_manifest(
        {
            "schema_version": SCHEMA_VERSION,
            "benchmark_version": BENCHMARK_VERSION,
            "dataset": dataset,
            "backend": "snowflake",
            "objects": objects,
        }
    )


def write_deployment_files(
    payload: dict[str, Any], *, state_dir: Path = DEFAULT_STATE_DIR
) -> tuple[Path, Path]:
    """Atomically write JSON and a sourceable, credential-free environment file."""

    validated = validate_deployment_manifest(payload)
    state_dir.mkdir(parents=True, exist_ok=True)
    dataset = validated["dataset"]
    manifest_path = state_dir / f"{dataset}.json"
    env_path = state_dir / f"{dataset}.env"
    _atomic_write(manifest_path, canonical_json(validated), 0o644)
    encoded = encode_deployment_manifest(validated)
    dataset_variable = "AVBENCH_" + dataset.upper().replace("-", "_")
    _atomic_write(
        env_path,
        (
            f"export AVBENCH_DEPLOYMENT_MANIFEST_B64={encoded}\n"
            f"export {dataset_variable}_DEPLOYMENT_MANIFEST_B64={encoded}\n"
        ).encode(),
        0o600,
    )
    return manifest_path, env_path


def _atomic_write(path: Path, content: bytes, mode: int) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
