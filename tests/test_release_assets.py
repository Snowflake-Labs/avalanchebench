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

import importlib
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfWriter
from pypdf.generic import (
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
)


@pytest.fixture
def scanner(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "scripts"))
    return importlib.import_module("release_check")


def compressed_pdf(path: Path) -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 20 Tf 72 720 Td (Cresta Field) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream.flate_encode())
    with path.open("wb") as output:
        writer.write(output)


def test_compressed_pdf_brand_is_detected(scanner, tmp_path):
    path = tmp_path / "compressed.pdf"
    compressed_pdf(path)
    assert b"Cresta" not in path.read_bytes()
    from release_assets import document_content

    if not shutil.which("tesseract"):
        pytest.skip("requires system Tesseract")
    with pytest.raises(RuntimeError, match="cresta"):
        for content in document_content(path):
            scanner._scan_bytes(content, path.name)


def test_raster_pdf_brand_is_detected(scanner, tmp_path):
    if not shutil.which("tesseract"):
        pytest.skip("requires system Tesseract")
    image = Image.new("RGB", (1000, 400), "white")
    ImageDraw.Draw(image).text(
        (50, 80), "Cresta Field", fill="black", font=ImageFont.load_default(size=70)
    )
    path = tmp_path / "raster.pdf"
    image.save(path, format="PDF")
    from release_assets import document_content

    with pytest.raises(RuntimeError, match="cresta"):
        for content in document_content(path):
            scanner._scan_bytes(content, path.name)


def test_document_scan_fails_closed_without_ocr(scanner, tmp_path, monkeypatch):
    from release_assets import document_content

    path = tmp_path / "page.pdf"
    compressed_pdf(path)
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError, match="requires Tesseract"):
        document_content(path)


def test_compressed_pdf_xmp_metadata_is_scanned(scanner, tmp_path):
    if not shutil.which("tesseract"):
        pytest.skip("requires system Tesseract")
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    metadata = DecodedStreamObject()
    metadata.set_data(b"<metadata><title>Cresta Field</title></metadata>")
    writer._root_object[NameObject("/Metadata")] = writer._add_object(
        metadata.flate_encode()
    )
    path = tmp_path / "metadata.pdf"
    with path.open("wb") as output:
        writer.write(output)
    assert b"Cresta" not in path.read_bytes()
    from release_assets import document_content

    with pytest.raises(RuntimeError, match="cresta"):
        for content in document_content(path):
            scanner._scan_bytes(content, path.name)


def test_office_split_text_runs_are_scanned(scanner, tmp_path):
    path = tmp_path / "example.docx"
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("word/document.xml", "<p><t>Cre</t><t>sta Field</t></p>")
    from release_assets import document_content

    with pytest.raises(RuntimeError, match="cresta"):
        for content in document_content(path):
            scanner._scan_bytes(content, path.name)


def test_history_account_url_placeholders_are_narrow(scanner):
    pattern = scanner.FORBIDDEN_HISTORY_TEXT["internal Snowflake account URL"]
    assert not pattern.search(b"https://example.snowflakecomputing.com")
    assert pattern.search(b"https://private-account." + b"snowflake" + b"computing.com")


def test_generic_legacy_task_identifier_is_blocked_without_private_names(scanner):
    identifier = "-".join(["example", "task", "snowflake", "v7"])
    assert scanner.FORBIDDEN_PROMPT.search(identifier)
    assert scanner.FORBIDDEN_HISTORY_TEXT["internal task source name"].search(
        identifier.encode()
    )
    assert not scanner.FORBIDDEN_PROMPT.search("avalanchebench-ecommerce")


@pytest.fixture
def single_commit_release(scanner, tmp_path, monkeypatch):
    origin = Path(__file__).parents[1]
    runtime_requirements = scanner.exported_runtime_requirements()
    root = tmp_path / "release"
    root.mkdir()
    for directory in ["src", "tasks"]:
        shutil.copytree(
            origin / directory,
            root / directory,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    (root / "data").mkdir()
    shutil.copy2(origin / "data/manifest.json", root / "data/manifest.json")
    shutil.copy2(origin / "dataset.toml", root / "dataset.toml")
    subprocess.run(["git", "init", "-q", "--initial-branch=main"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    refresher = importlib.import_module("refresh_release_metadata")
    monkeypatch.setattr(refresher, "ROOT", root)
    monkeypatch.setattr(sys, "argv", ["refresh_release_metadata.py", "--staged"])
    assert refresher.main() == 0
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Snowflake Inc.",
            "-c",
            "user.email=opensource@snowflake.com",
            "commit",
            "-q",
            "-m",
            "Single publication snapshot",
        ],
        cwd=root,
        check=True,
    )
    monkeypatch.setattr(scanner, "ROOT", root)
    return root, runtime_requirements


def test_source_tree_provenance_survives_a_single_commit_fresh_clone(
    scanner, single_commit_release, tmp_path, monkeypatch
):
    root, requirements = single_commit_release
    clone = tmp_path / "fresh-clone"
    subprocess.run(
        ["git", "clone", "--quiet", "--no-hardlinks", str(root), str(clone)],
        check=True,
    )
    assert (
        subprocess.check_output(
            ["git", "rev-list", "--count", "--all"], cwd=clone
        ).strip()
        == b"1"
    )
    monkeypatch.setattr(scanner, "ROOT", clone)
    tree = (
        subprocess.check_output(
            ["git", "rev-parse", "HEAD:src/avalanche_bench"], cwd=clone
        )
        .decode()
        .strip()
    )
    for task in scanner.TASKS:
        release = json.loads(
            (clone / "tasks" / task / "tests/release.json").read_text()
        )
        assert release["evaluator_source_tree"] == tree
        assert "evaluator_source_commit" not in release
        scanner.check_task(task, requirements)
    scanner.check_dataset_manifest()


def test_release_checker_accepts_legacy_commit_provenance(
    scanner, single_commit_release
):
    root, requirements = single_commit_release
    path = root / "tasks/ecommerce/tests/release.json"
    release = json.loads(path.read_text())
    release.pop("evaluator_source_tree")
    release["evaluator_source_commit"] = (
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root).decode().strip()
    )
    path.write_text(json.dumps(release))
    scanner.check_task("ecommerce", requirements)


def test_release_checker_rejects_a_tree_other_than_the_published_source(
    scanner, single_commit_release
):
    root, requirements = single_commit_release
    path = root / "tasks/ecommerce/tests/release.json"
    release = json.loads(path.read_text())
    release["evaluator_source_tree"] = (
        subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=root)
        .decode()
        .strip()
    )
    path.write_text(json.dumps(release))
    with pytest.raises(RuntimeError, match="source tree differs"):
        scanner.check_task("ecommerce", requirements)


def test_release_checker_rejects_changed_evaluator_bytes(
    scanner, single_commit_release
):
    root, requirements = single_commit_release
    path = root / "src/avalanche_bench/evaluation.py"
    path.write_text(path.read_text() + "\n# altered after provenance pinning\n")
    with pytest.raises(RuntimeError, match="evaluator_sha256 is stale"):
        scanner.check_task("ecommerce", requirements)
