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

import json
import stat
from pathlib import Path

import pytest

from avalanche_bench.manifests import (
    ManifestError,
    decode_deployment_manifest,
    encode_deployment_manifest,
    materialize_deployment_manifest,
    validate_deployment_manifest,
)


def alternate_manifest() -> dict:
    return {
        "schema_version": "0.1.0",
        "benchmark_version": "0.1.0",
        "dataset": "ecommerce",
        "backend": "bigquery",
        "objects": {
            "products": {"kind": "table", "identifier": "project.dataset.products"},
            "reviews": {"kind": "table", "identifier": "project.dataset.reviews"},
            "sales": {"kind": "table", "identifier": "project.dataset.sales"},
        },
    }


def test_contract_accepts_alternate_backend_identifiers() -> None:
    assert validate_deployment_manifest(alternate_manifest())["backend"] == "bigquery"


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("password",), "not-a-real-secret"),
        (("objects", "products", "token"), "not-a-real-token"),
        (("objects", "products", "identifier"), "https://account.example/table"),
    ],
)
def test_contract_rejects_credentials_and_urls(
    path: tuple[str, ...], value: str
) -> None:
    payload = alternate_manifest()
    target = payload
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    with pytest.raises(ManifestError):
        validate_deployment_manifest(payload)


def test_contract_requires_complete_object_set() -> None:
    payload = alternate_manifest()
    del payload["objects"]["sales"]
    with pytest.raises(ManifestError, match="missing logical objects: sales"):
        validate_deployment_manifest(payload)


def test_environment_payload_materializes_read_only(tmp_path: Path) -> None:
    payload = alternate_manifest()
    encoded = encode_deployment_manifest(payload)
    destination = tmp_path / "avalanche_dataset.json"
    materialize_deployment_manifest(
        destination, expected_dataset="ecommerce", encoded=encoded
    )
    assert json.loads(destination.read_text()) == validate_deployment_manifest(payload)
    assert stat.S_IMODE(destination.stat().st_mode) == 0o444
    assert (
        decode_deployment_manifest(encoded, expected_dataset="ecommerce")["dataset"]
        == "ecommerce"
    )


def test_environment_payload_rejects_wrong_dataset(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="does not match"):
        materialize_deployment_manifest(
            tmp_path / "manifest.json",
            expected_dataset="travel",
            encoded=encode_deployment_manifest(alternate_manifest()),
        )
