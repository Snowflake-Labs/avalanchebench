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

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from avalanche_bench.data import (
    DataError,
    SnowflakeLoader,
    archive_member_record,
    deterministic_zip,
    extract_and_verify_archive,
)


@pytest.mark.parametrize(
    ("suffix", "media_type"),
    [
        (".csv", "text/csv"),
        (
            ".docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        (
            ".pptx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ),
    ],
)
def test_archive_media_types_are_pinned(
    tmp_path: Path, suffix: str, media_type: str
) -> None:
    path = tmp_path / f"member{suffix}"
    path.write_bytes(b"data")
    assert (
        archive_member_record(path, path.name, role="document")["media_type"]
        == media_type
    )


def test_unmapped_archive_media_type_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "member.unknown"
    path.write_bytes(b"data")
    with pytest.raises(DataError, match="unsupported archive member type"):
        archive_member_record(path, path.name, role="document")


class FakeSnowflakeError(Exception):
    def __init__(self, message: str, *, errno: int) -> None:
        super().__init__(message)
        self.errno = errno


def manifest(member: Path) -> dict:
    return {
        "schema_version": "0.1.0",
        "benchmark_version": "0.1.0",
        "dataset": "ecommerce",
        "members": [
            {
                "path": "tables/products.csv",
                "bytes": member.stat().st_size,
                "sha256": hashlib.sha256(member.read_bytes()).hexdigest(),
                "media_type": "text/csv",
                "role": "table",
            }
        ],
        "tables": {
            "products": {"file": "tables/products.csv"},
            "reviews": {"file": "tables/products.csv"},
            "sales": {"file": "tables/products.csv"},
        },
        "stages": {},
    }


def test_deterministic_archive_round_trip(tmp_path: Path) -> None:
    member = tmp_path / "products.csv"
    member.write_text("id,name\n1,item\n")
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"
    payload = manifest(member)
    deterministic_zip(first, payload, [(member, "tables/products.csv")])
    deterministic_zip(second, payload, [(member, "tables/products.csv")])
    assert first.read_bytes() == second.read_bytes()
    extracted = tmp_path / "extracted"
    assert extract_and_verify_archive(first, extracted)["dataset"] == "ecommerce"
    assert (extracted / "tables/products.csv").read_bytes() == member.read_bytes()


def test_traversal_member_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("../outside", "bad")
        handle.writestr("manifest.json", json.dumps({}))
    with pytest.raises(DataError, match="unsafe archive member"):
        extract_and_verify_archive(archive, tmp_path / "out")


def test_missing_snowflake_object_is_not_reported_as_existing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loader = SnowflakeLoader(object())

    def missing_object(_sql: str) -> None:
        raise FakeSnowflakeError("does not exist", errno=2003)

    monkeypatch.setattr(loader, "_execute", missing_object)
    assert not loader._object_exists("TABLE", "DATABASE.SCHEMA.TABLE")


def test_snowflake_permission_error_is_not_reported_as_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loader = SnowflakeLoader(object())

    def permission_error(_sql: str) -> None:
        raise FakeSnowflakeError("insufficient privileges", errno=3001)

    monkeypatch.setattr(loader, "_execute", permission_error)
    with pytest.raises(FakeSnowflakeError, match="insufficient privileges"):
        loader._object_exists("TABLE", "DATABASE.SCHEMA.TABLE")


def test_inaccessible_database_has_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loader = SnowflakeLoader(object())

    def database_error(_sql: str) -> None:
        raise FakeSnowflakeError("insufficient privileges", errno=3001)

    monkeypatch.setattr(loader, "_execute", database_error)
    with pytest.raises(DataError, match="ask an administrator to pre-create it"):
        loader._ensure_namespace("DATABASE", "SCHEMA")
