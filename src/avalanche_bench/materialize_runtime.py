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

"""Container entry point that materializes the runtime data-location contract."""

from __future__ import annotations

import argparse
from pathlib import Path

from .manifests import ManifestError, materialize_deployment_manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset")
    parser.add_argument(
        "--destination",
        type=Path,
        default=Path("/app/avalanche_dataset.json"),
    )
    args = parser.parse_args()
    try:
        materialize_deployment_manifest(
            args.destination,
            expected_dataset=args.dataset,
        )
    except ManifestError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
