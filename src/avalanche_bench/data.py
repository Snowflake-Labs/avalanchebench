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

"""Archive verification and the Snowflake reference loader."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from .deployment import snowflake_manifest, write_deployment_files
from .manifests import BENCHMARK_VERSION, REQUIRED_OBJECTS, SCHEMA_VERSION

_LFS_HEADER = b"version https://git-lfs.github.com/spec/v1\n"
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


class DataError(RuntimeError):
    """A release archive or loaded Snowflake object failed validation."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_lfs_pointer(path: Path) -> bool:
    with path.open("rb") as handle:
        return handle.read(len(_LFS_HEADER)) == _LFS_HEADER


def _safe_member(info: zipfile.ZipInfo) -> PurePosixPath:
    if "\\" in info.filename:
        raise DataError(f"archive member uses a backslash: {info.filename!r}")
    path = PurePosixPath(info.filename)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise DataError(f"unsafe archive member path: {info.filename!r}")
    file_type = (info.external_attr >> 16) & 0o170000
    if file_type == stat.S_IFLNK:
        raise DataError(f"archive member is a symlink: {info.filename!r}")
    if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
        raise DataError(f"archive member has unsupported type: {info.filename!r}")
    return path


def extract_and_verify_archive(archive: Path, destination: Path) -> dict[str, Any]:
    """Safely extract an archive and verify every declared data member."""

    try:
        handle = zipfile.ZipFile(archive)
    except zipfile.BadZipFile as exc:
        raise DataError(f"invalid ZIP archive: {archive}") from exc
    with handle:
        infos = handle.infolist()
        if len({info.filename for info in infos}) != len(infos):
            raise DataError("archive contains duplicate member names")
        paths = {_safe_member(info): info for info in infos}
        manifest_path = PurePosixPath("manifest.json")
        if manifest_path not in paths:
            raise DataError("archive has no manifest.json")
        try:
            manifest = json.loads(handle.read("manifest.json"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DataError("archive manifest is not valid UTF-8 JSON") from exc
        validate_archive_manifest(manifest)
        declared = {PurePosixPath(item["path"]): item for item in manifest["members"]}
        actual = {path for path, info in paths.items() if not info.is_dir()} - {
            manifest_path
        }
        if set(declared) != actual:
            missing = sorted(str(item) for item in set(declared) - actual)
            extra = sorted(str(item) for item in actual - set(declared))
            raise DataError(
                f"archive member mismatch; missing={missing}, extra={extra}"
            )
        destination.mkdir(parents=True, exist_ok=True)
        root = destination.resolve()
        for path, info in paths.items():
            target = (destination / Path(*path.parts)).resolve()
            if not target.is_relative_to(root):
                raise DataError(f"archive member escapes destination: {path}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with handle.open(info) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            os.chmod(target, 0o644)
            if path == manifest_path:
                continue
            expected = declared[path]
            if target.stat().st_size != expected["bytes"]:
                raise DataError(f"size mismatch for {path}")
            if sha256_file(target) != expected["sha256"]:
                raise DataError(f"SHA-256 mismatch for {path}")
        return manifest


def validate_archive_manifest(payload: Mapping[str, Any]) -> None:
    if not isinstance(payload, Mapping):
        raise DataError("archive manifest must be a JSON object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise DataError(f"archive schema_version must be {SCHEMA_VERSION}")
    if payload.get("benchmark_version") != BENCHMARK_VERSION:
        raise DataError(f"archive benchmark_version must be {BENCHMARK_VERSION}")
    dataset = payload.get("dataset")
    if dataset not in REQUIRED_OBJECTS:
        raise DataError(f"unknown archive dataset {dataset!r}")
    members = payload.get("members")
    if not isinstance(members, list) or not members:
        raise DataError("archive manifest members must be a non-empty list")
    member_paths: set[str] = set()
    for index, item in enumerate(members):
        if not isinstance(item, Mapping):
            raise DataError(f"members[{index}] is not an object")
        path = item.get("path")
        if not isinstance(path, str):
            raise DataError(f"members[{index}].path is required")
        _safe_member(zipfile.ZipInfo(path))
        if path == "manifest.json" or path in member_paths:
            raise DataError(f"invalid or duplicate declared member {path!r}")
        member_paths.add(path)
        if not isinstance(item.get("bytes"), int) or item["bytes"] < 0:
            raise DataError(f"members[{index}].bytes is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256", ""))):
            raise DataError(f"members[{index}].sha256 is invalid")
        if not isinstance(item.get("media_type"), str) or not item["media_type"]:
            raise DataError(f"members[{index}].media_type is required")
    tables = payload.get("tables")
    stages = payload.get("stages", {})
    if not isinstance(tables, Mapping) or not isinstance(stages, Mapping):
        raise DataError("tables and stages must be JSON objects")
    logical = set(tables) | set(stages)
    if logical != set(REQUIRED_OBJECTS[dataset]):
        raise DataError("archive logical objects do not match the dataset contract")


def verify_release_archive(data_dir: Path, dataset: str) -> tuple[Path, dict[str, Any]]:
    root_path = data_dir / "manifest.json"
    try:
        root = json.loads(root_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DataError(f"release manifest not found: {root_path}") from exc
    except json.JSONDecodeError as exc:
        raise DataError("root data manifest is invalid JSON") from exc
    if root.get("benchmark_version") != BENCHMARK_VERSION:
        raise DataError(f"root manifest benchmark_version must be {BENCHMARK_VERSION}")
    try:
        record = root["archives"][dataset]
    except (KeyError, TypeError) as exc:
        raise DataError(f"root manifest has no archive for {dataset}") from exc
    archive = data_dir / record["path"]
    if not archive.exists():
        raise DataError(f"archive not found: {archive}")
    if is_lfs_pointer(archive):
        pull_path = archive.as_posix()
        raise DataError(
            f"{archive} is a Git LFS pointer; run `git lfs pull --include={pull_path}`"
        )
    if archive.stat().st_size != record["bytes"]:
        raise DataError(f"outer archive size mismatch for {dataset}")
    if sha256_file(archive) != record["sha256"]:
        raise DataError(f"outer archive SHA-256 mismatch for {dataset}")
    return archive, record


def _identifier(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise DataError(f"unsupported Snowflake identifier: {value!r}")
    return value.upper()


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _fq(database: str, schema: str, name: str) -> str:
    return ".".join(_identifier(part) for part in (database, schema, name))


class SnowflakeLoader:
    """Load one verified release archive into caller-owned Snowflake objects."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def load(
        self,
        *,
        archive: Path,
        database: str,
        schema: str,
        replace: bool,
        state_dir: Path,
    ) -> tuple[Path, Path]:
        database = _identifier(database)
        schema = _identifier(schema)
        try:
            with tempfile.TemporaryDirectory(prefix="avbench-load-") as temporary:
                extracted = Path(temporary) / "archive"
                manifest = extract_and_verify_archive(archive, extracted)
                dataset = manifest["dataset"]
                self._ensure_namespace(database, schema)
                self._preflight_objects(manifest, database, schema, replace=replace)
                for logical_name, table in manifest["tables"].items():
                    self._load_table(
                        logical_name,
                        table,
                        extracted,
                        database,
                        schema,
                        replace=replace,
                    )
                for logical_name, stage in manifest.get("stages", {}).items():
                    self._load_stage(
                        logical_name,
                        stage,
                        extracted,
                        database,
                        schema,
                        replace=replace,
                    )
                self._validate_loaded(manifest, database, schema, Path(temporary))
                payload = snowflake_manifest(dataset, database=database, schema=schema)
                return write_deployment_files(payload, state_dir=state_dir)
        except DataError:
            raise
        except Exception as exc:
            raise DataError(f"Snowflake setup failed: {exc}") from exc

    def _execute(self, sql: str) -> Any:
        cursor = self.connection.cursor()
        try:
            cursor.execute(sql)
            return cursor.fetchall() if cursor.description else []
        finally:
            cursor.close()

    def _ensure_namespace(self, database: str, schema: str) -> None:
        try:
            self._execute(f"USE DATABASE {database}")
        except Exception:
            try:
                self._execute(f"CREATE DATABASE {database}")
            except Exception as create_error:
                raise DataError(
                    f"database {database} is not accessible and could not be created; "
                    "ask an administrator to pre-create it or pass --database for an "
                    "accessible database"
                ) from create_error
            self._execute(f"USE DATABASE {database}")
        self._execute(f"CREATE SCHEMA IF NOT EXISTS {database}.{schema}")
        self._execute(f"USE SCHEMA {database}.{schema}")

    def _object_exists(self, kind: str, fq_name: str) -> bool:
        try:
            self._execute(f"DESCRIBE {kind} {fq_name}")
            return True
        except Exception as exc:
            if getattr(exc, "errno", None) in {2003, 2043}:
                return False
            raise

    def _preflight_objects(
        self,
        manifest: Mapping[str, Any],
        database: str,
        schema: str,
        *,
        replace: bool,
    ) -> None:
        existing: list[str] = []
        for table in manifest["tables"].values():
            fq_name = _fq(database, schema, table["object_name"])
            if self._object_exists("TABLE", fq_name):
                existing.append(fq_name)
        for stage in manifest.get("stages", {}).values():
            fq_name = _fq(database, schema, stage["object_name"])
            if self._object_exists("STAGE", fq_name):
                existing.append("@" + fq_name)
        if existing and not replace:
            raise DataError(
                "refusing to overwrite existing benchmark objects: "
                + ", ".join(existing)
            )

    def _load_table(
        self,
        logical_name: str,
        table: Mapping[str, Any],
        extracted: Path,
        database: str,
        schema: str,
        *,
        replace: bool,
    ) -> None:
        fq_name = _fq(database, schema, table["object_name"])
        if replace:
            self._execute(f"DROP TABLE IF EXISTS {fq_name}")
        columns = table.get("columns")
        if not isinstance(columns, list) or not columns:
            raise DataError(f"table {logical_name} has no columns")
        column_sql = ", ".join(
            f"{_identifier(column['name'])} {column['snowflake_type']}"
            for column in columns
        )
        self._execute(f"CREATE TABLE {fq_name} ({column_sql})")
        source = extracted / table["file"]
        stage_name = _fq(database, schema, f"_AVBENCH_LOAD_{uuid4().hex[:16]}")
        self._execute(f"CREATE TEMPORARY STAGE {stage_name}")
        try:
            self._execute(
                f"PUT {_sql_string(source.resolve().as_uri())} @{stage_name} "
                "AUTO_COMPRESS=FALSE OVERWRITE=FALSE"
            )
            if table["format"] == "csv":
                copy = (
                    f"COPY INTO {fq_name} FROM @{stage_name} "
                    "FILE_FORMAT=(TYPE=CSV SKIP_HEADER=1 "
                    "FIELD_OPTIONALLY_ENCLOSED_BY='\"' "
                    "NULL_IF=('')) ON_ERROR=ABORT_STATEMENT"
                )
            elif table["format"] == "parquet":
                select = ", ".join(
                    f"$1:{_identifier(column['name'])}::{column['snowflake_type']}"
                    for column in columns
                )
                copy = (
                    f"COPY INTO {fq_name} FROM (SELECT {select} FROM @{stage_name}) "
                    "FILE_FORMAT=(TYPE=PARQUET) ON_ERROR=ABORT_STATEMENT"
                )
            else:
                raise DataError(f"unsupported table format for {logical_name}")
            self._execute(copy)
        finally:
            self._execute(f"DROP STAGE IF EXISTS {stage_name}")

    def _load_stage(
        self,
        logical_name: str,
        stage: Mapping[str, Any],
        extracted: Path,
        database: str,
        schema: str,
        *,
        replace: bool,
    ) -> None:
        fq_name = _fq(database, schema, stage["object_name"])
        if replace:
            self._execute(f"DROP STAGE IF EXISTS {fq_name}")
        self._execute(
            f"CREATE STAGE {fq_name} DIRECTORY=(ENABLE=TRUE) "
            "ENCRYPTION=(TYPE='SNOWFLAKE_SSE')"
        )
        paths = stage.get("members")
        if not isinstance(paths, list) or not paths:
            raise DataError(f"stage {logical_name} has no members")
        sources = [(extracted / member).resolve() for member in paths]
        parents = {source.parent for source in sources}
        if len(parents) != 1:
            raise DataError(f"stage {logical_name} members must share one directory")
        parent = parents.pop()
        actual_names = {path.name for path in parent.iterdir() if path.is_file()}
        expected_names = {source.name for source in sources}
        if actual_names != expected_names:
            raise DataError(
                f"stage {logical_name} extraction directory has extra files"
            )
        source_pattern = parent.as_uri() + "/*"
        self._execute(
            f"PUT {_sql_string(source_pattern)} @{fq_name} "
            "AUTO_COMPRESS=FALSE OVERWRITE=FALSE PARALLEL=16"
        )

    def _validate_loaded(
        self,
        manifest: Mapping[str, Any],
        database: str,
        schema: str,
        temporary: Path,
    ) -> None:
        for logical_name, table in manifest["tables"].items():
            fq_name = _fq(database, schema, table["object_name"])
            rows = self._execute(f"SELECT COUNT(*), HASH_AGG(*) FROM {fq_name}")
            if not rows or int(rows[0][0]) != int(table["rows"]):
                raise DataError(f"row-count mismatch for {logical_name}")
            if int(rows[0][1]) != int(table["snowflake_hash_agg"]):
                raise DataError(f"Snowflake HASH_AGG mismatch for {logical_name}")
            described = self._execute(f"DESCRIBE TABLE {fq_name}")
            actual = [(str(row[0]).upper(), str(row[1]).upper()) for row in described]
            expected = [
                (_identifier(column["name"]), str(column["snowflake_type"]).upper())
                for column in table["columns"]
            ]
            if actual != expected:
                raise DataError(f"column-schema mismatch for {logical_name}")

        member_index = {item["path"]: item for item in manifest["members"]}
        for logical_name, stage in manifest.get("stages", {}).items():
            fq_name = _fq(database, schema, stage["object_name"])
            listed = self._execute(f"LIST @{fq_name}")
            if len(listed) != int(stage["files"]):
                raise DataError(f"file-count mismatch for {logical_name}")
            if sum(int(row[1]) for row in listed) != int(stage["bytes"]):
                raise DataError(f"byte-count mismatch for {logical_name}")
            listed_names = {PurePosixPath(str(row[0])).name for row in listed}
            expected_names = {PurePosixPath(member).name for member in stage["members"]}
            if listed_names != expected_names:
                raise DataError(f"filename mismatch for {logical_name}")
            download = temporary / f"validate-{logical_name}"
            download.mkdir()
            self._execute(
                f"GET @{fq_name} {_sql_string(download.resolve().as_uri())} PARALLEL=16"
            )
            for member in stage["members"]:
                name = PurePosixPath(member).name
                matches = list(download.rglob(name))
                if (
                    len(matches) != 1
                    or sha256_file(matches[0]) != member_index[member]["sha256"]
                ):
                    raise DataError(f"checksum mismatch for staged file {name}")


def archive_member_record(path: Path, relative: str, *, role: str) -> dict[str, Any]:
    """Build a stable member record for the release packaging script."""

    media_types = {
        ".pdf": "application/pdf",
        ".docx": (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        ".pptx": (
            "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        ),
        ".csv": "text/csv",
        ".jpg": "image/jpeg",
    }
    try:
        media_type = media_types[path.suffix.lower()]
    except KeyError as exc:
        raise DataError(f"unsupported archive member type: {path.suffix}") from exc
    return {
        "path": relative,
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "media_type": media_type,
        "role": role,
    }


def deterministic_zip(
    destination: Path,
    manifest: Mapping[str, Any],
    files: Iterable[tuple[Path, str]],
) -> None:
    """Write stable ZIP bytes for an already-frozen allowlist of files."""

    timestamp = (1980, 1, 1, 0, 0, 0)
    with zipfile.ZipFile(
        destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        manifest_info = zipfile.ZipInfo("manifest.json", timestamp)
        manifest_info.create_system = 3
        manifest_info.external_attr = 0o100644 << 16
        manifest_info.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(
            manifest_info,
            (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(),
        )
        for source, relative in sorted(files, key=lambda item: item[1]):
            info = zipfile.ZipInfo(relative, timestamp)
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            with source.open("rb") as handle:
                archive.writestr(info, handle.read())
