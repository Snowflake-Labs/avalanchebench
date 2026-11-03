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

"""Package the frozen, explicitly allowlisted release inputs deterministically."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
from typing import Any

from avalanche_bench.data import (
    archive_member_record,
    deterministic_zip,
    sha256_file,
    validate_archive_manifest,
)

VERSION = "0.1.0"
KNOWLEDGE_BASE_PATH_SHA256 = "".join(
    (
        "0ae6f9c1",
        "02cd4707",
        "68093652",
        "954bdc0a",
        "09bdc572",
        "6c719f19",
        "7596007b",
        "1154cdf1",
    )
)


def columns(*definitions: tuple[str, str]) -> list[dict[str, str]]:
    return [
        {"name": name, "snowflake_type": snowflake_type}
        for name, snowflake_type in definitions
    ]


VARCHAR = "VARCHAR(16777216)"
NUMBER = "NUMBER(38,0)"
TABLES: dict[str, dict[str, dict[str, Any]]] = {
    "ecommerce": {
        "products": {
            "object_name": "PRODUCTS",
            "file": "tables/products.csv",
            "format": "csv",
            "rows": 20,
            "snowflake_hash_agg": 7108356714745069108,
            "columns": columns(
                ("PRODUCT_ID", NUMBER),
                ("PRODUCT_NAME", VARCHAR),
                ("CATEGORY", VARCHAR),
                ("GENDER", VARCHAR),
                ("DESCRIPTION", VARCHAR),
            ),
        },
        "reviews": {
            "object_name": "REVIEWS",
            "file": "tables/reviews.csv",
            "format": "csv",
            "rows": 20000,
            "snowflake_hash_agg": 6385935392178789555,
            "columns": columns(
                ("REVIEW_ID", NUMBER),
                ("CUSTOMER_ID", NUMBER),
                ("OVERALL_RATING", "FLOAT"),
                ("REVIEW_MONTH", NUMBER),
                ("REVIEW_DATE", VARCHAR),
                ("REVIEW_TEXT", VARCHAR),
            ),
        },
        "sales": {
            "object_name": "SALES",
            "file": "tables/sales.csv",
            "format": "csv",
            "rows": 28221,
            "snowflake_hash_agg": 7243436048326116027,
            "columns": columns(
                ("TRANSACTION_ID", VARCHAR),
                ("CUSTOMER_ID", NUMBER),
                ("PRODUCT_NAME", VARCHAR),
                ("QUANTITY", NUMBER),
                ("UNIT_PRICE", "FLOAT"),
                ("TOTAL_AMOUNT", "FLOAT"),
                ("TRANSACTION_DATE", VARCHAR),
                ("SALES_CHANNEL", VARCHAR),
                ("DISCOUNT_APPLIED", "FLOAT"),
            ),
        },
    },
    "customer-support": {
        "tickets": {
            "object_name": "TICKETS",
            "file": "tables/tickets.csv",
            "format": "csv",
            "rows": 5000,
            "snowflake_hash_agg": -4125551336199381652,
            "columns": columns(
                ("TICKET_ID", VARCHAR),
                ("OPENED_AT", VARCHAR),
                ("RESOLVED_AT", VARCHAR),
                ("CHANNEL", VARCHAR),
                ("PRIORITY", VARCHAR),
                ("STATE", VARCHAR),
                ("RELEASE_WAVE", VARCHAR),
                ("ACCOUNT_NAME", VARCHAR),
                ("REQUESTER_ROLE", VARCHAR),
                ("CUSTOMER_TIER", VARCHAR),
                ("REGION", VARCHAR),
                ("PRODUCT_AREA", VARCHAR),
                ("CATEGORY", VARCHAR),
                ("SUBCATEGORY", VARCHAR),
                ("ASSIGNMENT_GROUP", VARCHAR),
                ("REASSIGNMENT_COUNT", NUMBER),
                ("REOPEN_COUNT", NUMBER),
                ("FIRST_RESPONSE_MINUTES", NUMBER),
                ("RESOLUTION_MINUTES", "FLOAT"),
                ("SHORT_DESCRIPTION", VARCHAR),
                ("DESCRIPTION", VARCHAR),
                ("BUSINESS_IMPACT", VARCHAR),
                ("RESOLUTION_CODE", VARCHAR),
                ("CLOSE_NOTES", VARCHAR),
            ),
        },
        "ticket_comments": {
            "object_name": "TICKET_COMMENTS",
            "file": "tables/ticket_comments.csv",
            "format": "csv",
            "rows": 19653,
            "snowflake_hash_agg": -1076992608229498440,
            "columns": columns(
                ("TICKET_ID", VARCHAR),
                ("COMMENT_AT", VARCHAR),
                ("AUTHOR_ROLE", VARCHAR),
                ("AUTHOR_TEAM", VARCHAR),
                ("VISIBILITY", VARCHAR),
                ("NOTE_TYPE", VARCHAR),
                ("BODY", VARCHAR),
                ("COMMENT_ID", VARCHAR),
            ),
        },
    },
    "travel": {
        "destinations": {
            "object_name": "DESTINATIONS",
            "file": "tables/destinations.csv",
            "format": "csv",
            "rows": 8,
            "snowflake_hash_agg": 1375298449384200617,
            "columns": columns(
                ("DESTINATION_ID", NUMBER),
                ("DESTINATION_NAME", VARCHAR),
                ("REGION", VARCHAR),
                ("PEAK_MONTHS", VARCHAR),
                ("OFF_PEAK_MONTHS", VARCHAR),
                ("PRIMARY_CHANNEL", VARCHAR),
            ),
        },
        "campaigns": {
            "object_name": "CAMPAIGNS",
            "file": "tables/campaigns.csv",
            "format": "csv",
            "rows": 8,
            "snowflake_hash_agg": 1557297094015628315,
            "columns": columns(
                ("CAMPAIGN_ID", NUMBER),
                ("CAMPAIGN_NAME", VARCHAR),
                ("DESTINATION_NAME", VARCHAR),
                ("PRIMARY_CHANNEL", VARCHAR),
                ("BUDGET_USD", NUMBER),
                ("START_MONTH", NUMBER),
                ("END_MONTH", NUMBER),
                ("CAMPAIGN_TYPE", VARCHAR),
            ),
        },
        "images": {
            "object_name": "IMAGES",
            "file": "tables/images.csv",
            "format": "csv",
            "rows": 200,
            "snowflake_hash_agg": -820773260727252310,
            "columns": columns(
                ("IMAGE_ID", VARCHAR),
                ("CAMPAIGN_ID", NUMBER),
                ("DESTINATION_NAME", VARCHAR),
                ("CHANNEL", VARCHAR),
                ("IMAGE_FILENAME", VARCHAR),
            ),
        },
        "visitors": {
            "object_name": "VISITORS",
            "file": "tables/visitors.csv",
            "format": "csv",
            "rows": 5000,
            "snowflake_hash_agg": -6037722567263379860,
            "columns": columns(
                ("VISITOR_ID", NUMBER),
                ("AGE_GROUP", VARCHAR),
                ("AGE", NUMBER),
                ("ORIGIN_REGION", VARCHAR),
                ("HOME_CITY", VARCHAR),
                ("TRAVEL_FREQUENCY", NUMBER),
                ("PRIMARY_DEVICE", VARCHAR),
            ),
        },
        "interactions": {
            "object_name": "INTERACTIONS",
            "file": "tables/interactions.csv",
            "format": "csv",
            "rows": 654282,
            "snowflake_hash_agg": 6054311506255968110,
            "columns": columns(
                ("INTERACTION_ID", NUMBER),
                ("IMAGE_ID", VARCHAR),
                ("VISITOR_ID", NUMBER),
                ("CAMPAIGN_ID", NUMBER),
                ("DESTINATION_NAME", VARCHAR),
                ("CHANNEL", VARCHAR),
                ("MONTH", NUMBER),
                ("CLICKED", "BOOLEAN"),
                ("SAVED", "BOOLEAN"),
                ("BOOKING_INTENT_CLICKED", "BOOLEAN"),
                ("TIME_SPENT_SECONDS", NUMBER),
            ),
        },
        "comments": {
            "object_name": "COMMENTS",
            "file": "tables/comments.csv",
            "format": "csv",
            "rows": 8457,
            "snowflake_hash_agg": -1795142809288820100,
            "columns": columns(
                ("COMMENT_ID", NUMBER),
                ("IMAGE_ID", VARCHAR),
                ("VISITOR_ID", NUMBER),
                ("DESTINATION_NAME", VARCHAR),
                ("MONTH", NUMBER),
                ("COMMENT_TEXT", VARCHAR),
            ),
        },
    },
}


def csv_rows(path: Path) -> int:
    with path.open(newline="", encoding="utf-8") as handle:
        while True:
            position = handle.tell()
            if not handle.readline().startswith("#"):
                handle.seek(position)
                break
        return sum(1 for _ in csv.reader(handle)) - 1


def input_files(dataset: str, root: Path) -> list[tuple[Path, str, str]]:
    result = [
        (root / table["file"], table["file"], "table")
        for table in TABLES[dataset].values()
    ]
    if dataset == "customer-support":
        result.extend(
            (path, f"assets/knowledge_base/{path.name}", "document")
            for path in sorted((root / "knowledge_base").iterdir())
            if path.is_file()
        )
    elif dataset == "travel":
        result.extend(
            (path, f"assets/images/{path.name}", "image")
            for path in sorted((root / "assets/images").iterdir())
            if path.is_file()
        )
    return result


def build_manifest(
    dataset: str, root: Path
) -> tuple[dict[str, Any], list[tuple[Path, str]]]:
    files = input_files(dataset, root)
    for source, _relative, _role in files:
        if not source.is_file():
            raise RuntimeError(f"missing release input: {source}")
    members = [
        archive_member_record(source, relative, role=role)
        for source, relative, role in files
    ]
    tables = json.loads(json.dumps(TABLES[dataset]))
    for logical_name, table in tables.items():
        actual_rows = csv_rows(root / table["file"])
        if actual_rows != table["rows"]:
            expected_rows = table["rows"]
            raise RuntimeError(
                f"{dataset}.{logical_name}: expected {expected_rows} rows, "
                f"got {actual_rows}"
            )
        table["source_identity"] = (
            f"AvalancheBench {VERSION} frozen {logical_name} snapshot"
        )
    stages: dict[str, Any] = {}
    if dataset == "customer-support":
        paths = [item[1] for item in files if item[2] == "document"]
        if (
            hashlib.sha256("\n".join(paths).encode()).hexdigest()
            != KNOWLEDGE_BASE_PATH_SHA256
        ):
            raise RuntimeError("customer-support document allowlist has drifted")
        stages["knowledge_base"] = {
            "object_name": "KNOWLEDGE_BASE",
            "members": paths,
            "files": len(paths),
            "bytes": sum(
                item["bytes"] for item in members if item["role"] == "document"
            ),
            "source_identity": "AvalancheBench 0.1.0 frozen knowledge-base corpus",
        }
    elif dataset == "travel":
        paths = [item[1] for item in files if item[2] == "image"]
        expected = [f"assets/images/image-{index:04d}.jpg" for index in range(1, 201)]
        if paths != expected:
            raise RuntimeError(
                "travel image allowlist is not the neutral 200-file sequence"
            )
        stages["image_assets"] = {
            "object_name": "IMAGES",
            "members": paths,
            "files": len(paths),
            "bytes": sum(item["bytes"] for item in members if item["role"] == "image"),
            "source_identity": "AvalancheBench 0.1.0 frozen image collection",
        }
    characteristics: dict[str, Any] = {}
    if dataset == "travel":
        characteristics["interaction_grain"] = {
            "unit": "one ad impression",
            "repeated_visitor_image_month_groups": 146871,
            "repeated_rows_beyond_first": 210108,
            "maximum_impressions_per_visitor_image_month": 13,
        }
    manifest = {
        "schema_version": VERSION,
        "benchmark_version": VERSION,
        "dataset": dataset,
        "dataset_version": VERSION,
        "members": members,
        "tables": tables,
        "stages": stages,
        "characteristics": characteristics,
        "provenance": {
            "release": f"AvalancheBench {VERSION}",
            "dataset_card": "docs/dataset-card.md",
            "license_and_attribution": "LICENSE and NOTICE",
        },
    }
    validate_archive_manifest(manifest)
    return manifest, [(source, relative) for source, relative, _role in files]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("data"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    root_manifest: dict[str, Any] = {
        "schema_version": VERSION,
        "benchmark_version": VERSION,
        "archives": {},
    }
    for dataset in ("ecommerce", "customer-support", "travel"):
        manifest, files = build_manifest(dataset, args.inputs / dataset)
        destination = args.output / f"{dataset}.zip"
        deterministic_zip(destination, manifest, files)
        root_manifest["archives"][dataset] = {
            "path": destination.name,
            "bytes": destination.stat().st_size,
            "sha256": sha256_file(destination),
        }
    (args.output / "manifest.json").write_text(
        json.dumps(root_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
