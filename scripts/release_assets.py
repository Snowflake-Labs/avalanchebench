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

"""Extract document content for release scans, including rendered page text.

Uses permissively licensed Python libraries and the system Tesseract binary.
OCR is mandatory: searchable text alone misses screenshots and scanned pages.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path


def image_content(data: bytes) -> list[bytes]:
    from PIL import Image

    with Image.open(io.BytesIO(data)) as source:
        metadata = json.dumps(
            {"info": source.info, "exif": dict(source.getexif())}, default=str
        ).encode()
        raster = source.convert("RGB")
        output = io.BytesIO()
        raster.save(output, format="PNG")
    return [metadata, ocr(output.getvalue())]


def ocr(png: bytes) -> bytes:
    if not shutil.which("tesseract"):
        raise RuntimeError("release scan requires Tesseract with English language data")
    result = subprocess.run(
        ["tesseract", "stdin", "stdout", "-l", "eng"],
        input=png,
        capture_output=True,
        env={**os.environ, "OMP_THREAD_LIMIT": "1"},
        timeout=120,
        check=True,
    )
    return result.stdout


def document_content(path: Path) -> list[bytes]:
    """Return raw, decoded, metadata, and rendered text; errors fail the scan."""
    content = path.read_bytes()
    result = [content]
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        import pypdfium2 as pdfium
        from pypdf import PdfReader

        reader = PdfReader(path)
        if reader.is_encrypted:
            raise RuntimeError(f"encrypted release document: {path.name}")
        if reader.attachments:
            raise RuntimeError(f"release PDF contains attachments: {path.name}")
        result.append(str(reader.metadata).encode())
        if metadata := reader.trailer["/Root"].get("/Metadata"):
            xml = metadata.get_object().get_data()
            result.append(xml)
            result.append("".join(ET.fromstring(xml).itertext()).encode())
        for page in reader.pages:
            result.append((page.extract_text() or "").encode())
            result.append(str(page.get("/Annots", [])).encode())
        with pdfium.PdfDocument(path) as document:
            for page in document:
                try:
                    bitmap = page.render(scale=200 / 72)
                    try:
                        output = io.BytesIO()
                        bitmap.to_pil().save(output, format="PNG")
                        result.append(ocr(output.getvalue()))
                    finally:
                        bitmap.close()
                finally:
                    page.close()
    elif suffix in {".docx", ".pptx"}:
        with zipfile.ZipFile(path) as package:
            for name in package.namelist():
                if name.endswith("/"):
                    continue
                data = package.read(name)
                result.append(data)
                if name.endswith(".xml"):
                    # Join text runs so split words cannot evade the scan.
                    result.append("".join(ET.fromstring(data).itertext()).encode())
                elif "/media/" in name:
                    if Path(name).suffix.lower() in {".png", ".jpg", ".jpeg"}:
                        result.extend(image_content(data))
                    elif Path(name).suffix.lower() != ".svg":
                        raise RuntimeError(f"unsupported embedded image: {name}")
    elif suffix in {".jpg", ".jpeg", ".png"}:
        result.extend(image_content(content))
    return result
