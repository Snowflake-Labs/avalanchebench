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

import subprocess
from pathlib import Path

from avalanche_bench.deployment import snowflake_manifest, write_deployment_files


def test_setup_env_supports_individual_and_full_dataset_runs(tmp_path: Path) -> None:
    payload = snowflake_manifest("customer-support", database="BENCH", schema="SUPPORT")
    manifest_path, env_path = write_deployment_files(payload, state_dir=tmp_path)
    assert manifest_path.name == "customer-support.json"
    lines = env_path.read_text().splitlines()
    assert lines[0].startswith("export AVBENCH_DEPLOYMENT_MANIFEST_B64=")
    assert lines[1].startswith(
        "export AVBENCH_CUSTOMER_SUPPORT_DEPLOYMENT_MANIFEST_B64="
    )
    assert lines[0].split("=", 1)[1] == lines[1].split("=", 1)[1]


def test_sourcing_all_deployments_exports_each_task_manifest(tmp_path: Path) -> None:
    env_paths = []
    for dataset in ("ecommerce", "customer-support", "travel"):
        payload = snowflake_manifest(dataset, database="BENCH", schema="DATA")
        _, env_path = write_deployment_files(payload, state_dir=tmp_path)
        env_paths.append(env_path)

    environment = subprocess.check_output(
        [
            "bash",
            "-c",
            'for env_file in "$@"; do source "$env_file"; done; env',
            "bash",
            *(str(env_path) for env_path in env_paths),
        ],
        text=True,
    ).splitlines()
    for env_path in env_paths:
        expected = env_path.read_text().splitlines()[1].removeprefix("export ")
        assert expected in environment
