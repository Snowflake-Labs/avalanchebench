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

"""Validation for public release and runtime deployment manifests."""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "0.1.0"
BENCHMARK_VERSION = "0.1.0"

REQUIRED_OBJECTS: dict[str, dict[str, str]] = {
    "ecommerce": {
        "products": "table",
        "reviews": "table",
        "sales": "table",
    },
    "customer-support": {
        "tickets": "table",
        "ticket_comments": "table",
        "knowledge_base": "document_collection",
    },
    "travel": {
        "destinations": "table",
        "campaigns": "table",
        "images": "table",
        "visitors": "table",
        "interactions": "table",
        "comments": "table",
        "image_assets": "asset_collection",
    },
}

_KINDS = {"table", "document_collection", "asset_collection"}
_SECRET_KEY = re.compile(
    r"(?:^|_)(?:password|passwd|secret|token|pat|private_key|credential)s?(?:$|_)",
    re.IGNORECASE,
)
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_URL = re.compile(r"(?:https?|snowflake)://", re.IGNORECASE)


class ManifestError(ValueError):
    """A manifest is unsafe or violates the published contract."""


def canonical_json(payload: Mapping[str, Any]) -> bytes:
    """Return the stable UTF-8 representation used in environment payloads."""

    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()


def _reject_credentials(value: Any, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ManifestError(f"{path} contains a non-string key")
            if _SECRET_KEY.search(key):
                raise ManifestError(f"{path}.{key} is a credential-bearing field")
            _reject_credentials(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_credentials(child, f"{path}[{index}]")
    elif isinstance(value, str):
        if _PRIVATE_KEY.search(value):
            raise ManifestError(f"{path} contains private-key material")
        if _URL.search(value):
            raise ManifestError(
                f"{path} contains a URL; manifests store locations only"
            )


def validate_deployment_manifest(
    payload: Mapping[str, Any], *, expected_dataset: str | None = None
) -> dict[str, Any]:
    """Validate and normalize a credential-free deployment manifest."""

    if not isinstance(payload, Mapping):
        raise ManifestError("deployment manifest must be a JSON object")
    _reject_credentials(payload)

    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ManifestError(f"schema_version must be {SCHEMA_VERSION}")
    if payload.get("benchmark_version") != BENCHMARK_VERSION:
        raise ManifestError(f"benchmark_version must be {BENCHMARK_VERSION}")

    dataset = payload.get("dataset")
    if dataset not in REQUIRED_OBJECTS:
        raise ManifestError(f"unknown dataset: {dataset!r}")
    if expected_dataset is not None and dataset != expected_dataset:
        raise ManifestError(
            f"manifest dataset {dataset!r} does not match {expected_dataset!r}"
        )

    backend = payload.get("backend")
    if not isinstance(backend, str) or not backend.strip():
        raise ManifestError("backend must be a non-empty string")

    objects = payload.get("objects")
    if not isinstance(objects, Mapping):
        raise ManifestError("objects must be a JSON object")
    required = REQUIRED_OBJECTS[dataset]
    missing = sorted(set(required) - set(objects))
    if missing:
        raise ManifestError(f"missing logical objects: {', '.join(missing)}")

    normalized_objects: dict[str, dict[str, str]] = {}
    for logical_name, raw in objects.items():
        if not isinstance(logical_name, str) or not isinstance(raw, Mapping):
            raise ManifestError("every logical object must be a named JSON object")
        kind = raw.get("kind")
        identifier = raw.get("identifier")
        if kind not in _KINDS:
            raise ManifestError(f"objects.{logical_name}.kind is invalid")
        if logical_name in required and kind != required[logical_name]:
            raise ManifestError(
                f"objects.{logical_name}.kind must be {required[logical_name]}"
            )
        if not isinstance(identifier, str) or not identifier.strip():
            raise ManifestError(f"objects.{logical_name}.identifier is required")
        if any(char in identifier for char in ("\x00", "\r", "\n")):
            raise ManifestError(
                f"objects.{logical_name}.identifier has control characters"
            )
        normalized_objects[logical_name] = {
            "kind": str(kind),
            "identifier": identifier.strip(),
        }

    return {
        "schema_version": SCHEMA_VERSION,
        "benchmark_version": BENCHMARK_VERSION,
        "dataset": dataset,
        "backend": backend.strip(),
        "objects": normalized_objects,
    }


def encode_deployment_manifest(payload: Mapping[str, Any]) -> str:
    validated = validate_deployment_manifest(payload)
    return base64.b64encode(canonical_json(validated)).decode("ascii")


def decode_deployment_manifest(
    encoded: str, *, expected_dataset: str | None = None
) -> dict[str, Any]:
    if not encoded:
        raise ManifestError("AVBENCH_DEPLOYMENT_MANIFEST_B64 is required")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ManifestError("deployment manifest is not valid base64") from exc
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError("deployment manifest is not valid UTF-8 JSON") from exc
    return validate_deployment_manifest(payload, expected_dataset=expected_dataset)


def materialize_deployment_manifest(
    destination: Path,
    *,
    expected_dataset: str,
    encoded: str | None = None,
) -> dict[str, Any]:
    """Validate the environment payload and atomically write a read-only file."""

    payload = decode_deployment_manifest(
        encoded
        if encoded is not None
        else os.environ.get("AVBENCH_DEPLOYMENT_MANIFEST_B64", ""),
        expected_dataset=expected_dataset,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(canonical_json(payload))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o444)
        os.replace(temporary, destination)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return payload
