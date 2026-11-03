<!--
Copyright 2026 Snowflake Inc.
SPDX-License-Identifier: Apache-2.0

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
-->

# Data loading and deployment manifests

The reference loader verifies an archive before extraction, rejects traversal
and symlinks, creates the released tables/stages, validates every schema, row
count, `HASH_AGG`, staged byte count, filename, and checksum, and only then
writes `.avbench/deployments/<dataset>.json` and `.env`. Existing benchmark
objects are never overwritten unless `--replace` is explicit.

The loading role needs `USAGE` on its warehouse and target database, plus
`CREATE SCHEMA` on the database and `CREATE TABLE` and `CREATE STAGE` in the
target schema. A role loading into a pre-created schema needs the equivalent
ownership/grants there. `CREATE DATABASE` is needed only when the selected
database does not exist; use `--database` to select one an administrator has
already created.

## Deployment contract

The JSON contract is versioned independently of credentials:

```json
{
  "schema_version": "0.1.0",
  "benchmark_version": "0.1.0",
  "dataset": "travel",
  "backend": "snowflake",
  "objects": {
    "destinations": {
      "kind": "table",
      "identifier": "AVALANCHE_BENCH.TRAVEL.DESTINATIONS"
    },
    "image_assets": {
      "kind": "asset_collection",
      "identifier": "@AVALANCHE_BENCH.TRAVEL.IMAGES"
    }
  }
}
```

The real manifest contains every logical object required by that task. It must
not contain account URLs, passwords, tokens, private keys, or other credentials.
Connection authentication is passed separately through Harbor's environment
configuration.

Each generated `.env` exposes a generic
`AVBENCH_DEPLOYMENT_MANIFEST_B64` for an individual run and a dataset-specific
variable consumed by that dataset's `task.toml` during a full three-task run.
Harbor maps that host variable to the generic container variable. Before agent
execution, the task decodes and validates it and atomically materializes a
read-only `/app/avalanche_dataset.json`. Missing, wrong-version, wrong-dataset,
unsafe, or incomplete payloads fail before the agent starts. No archive bytes
enter a task container.

The location schema is backend-neutral. An alternate-system user may construct
the same logical object map with identifiers such as
`project.dataset.products` or `catalog.schema.sales`; the task prompt remains
unchanged. Release `0.1.0` does not ship an alternate loader, credential setup,
or Harbor environment adapter.
