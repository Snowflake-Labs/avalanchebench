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

# AvalancheBench

AvalancheBench measures whether an agent can turn large, heterogeneous
enterprise datasets into a decision-ready analytical report. Release `0.1.0`
contains three frozen synthetic use cases:

| Task | Data | Questions |
|---|---|---:|
| E-commerce | products, reviews, and sales | 15 |
| Customer Support | tickets, comments, and 900 mixed-format documents | 25 |
| Travel | six tables and 200 images | 18 |

The benchmark publishes its data, instructions, rubrics, reference answers,
judge prompts, aggregation, CATC cost metric, and monetary cost at pinned
provider list prices. It does not promise byte-identical agent output or
deterministic scores.

## Prerequisites

- Python 3.12, Git LFS 3+, `uv` 0.12.5+, Docker, and Harbor 0.22.0;
- a Snowflake account and role able to create schemas, tables, and stages in a
  caller-selected database (database creation is needed only when it is absent);
- a warehouse plus `SNOWFLAKE.CORTEX_REST_API_USER` or
  `SNOWFLAKE.CORTEX_USER` for the judges' effective role; and
- a programmatic access token or OAuth access token and availability of all
  three pinned judge aliases: `claude-opus-4-8`, `openai-gpt-5.4`, and
  `gemini-3.7-flash`.

## First run: e-commerce

Run these steps from a clone of this repository (set `GIT_LFS_SKIP_SMUDGE=1`
when cloning to avoid downloading all three archives). Use **your own**
Snowflake account: the archive contains data, not a hosted dataset or a
connection to the authors' account. Configure a named Snowflake Python
Connector connection (for example, `bench` in
`~/.snowflake/connections.toml`) for the loading role:

```toml
[bench]
account = "your-account-identifier"
user = "your-user"
authenticator = "externalbrowser"
role = "your-role"
warehouse = "your-warehouse"
```

Use your organization's supported authentication method if browser SSO is
unavailable. Keep credentials and tokens out of the repository. Install the
pinned runner, fetch just the small e-commerce archive, and load it into your
account (the default database is `AVALANCHE_BENCH`; pass `--database` if needed):

```bash
uv tool install harbor==0.22.0
git lfs install --local
git lfs pull --include=data/ecommerce.zip
uv sync --locked
uv run avbench setup --dataset ecommerce --connection bench
source .avbench/deployments/ecommerce.env
```

Setup validates the loaded tables and stages and writes the credential-free
deployment manifest under `.avbench/deployments/`. The task reads this manifest
as `/app/avalanche_dataset.json`; it does not assume a particular Snowflake
account or stage. E-commerce needs about 3.2 MB of data and 45 judge calls;
check warehouse and inference costs before running.

The named connection selects the **account and role**; setup creates or uses
database `AVALANCHE_BENCH` and schema `ECOMMERCE` by default. E-commerce loads
`PRODUCTS`, `REVIEWS`, and `SALES` there (no persistent stage). To choose a
different destination, add `--database MY_DATABASE --schema MY_SCHEMA` to the
setup command above. Support and travel default to schemas `CUSTOMER_SUPPORT`
and `TRAVEL`; their document/image stages are created in those schemas. Setup
refuses to overwrite existing benchmark tables or stages unless you explicitly
pass `--replace`.

Set agent and judge credentials for **your** account (replace every `your-*`
value). Use an account URL that resolves to your account. The named connection
above is for the local loader and CATC collector; Harbor passes these variables
separately to the agent and verifier. Judges call Cortex REST using the token
in `CORTEX_PAT` (also for OAuth) and its effective role, typically the user's
default role. `SNOWFLAKE_ROLE` applies to the agent, not the judges.

```bash
export SNOWFLAKE_ACCOUNT="your-account-identifier"
export SNOWFLAKE_USER="your-user"
export SNOWFLAKE_ROLE="your-role"
export SNOWFLAKE_WAREHOUSE="your-warehouse"
export SNOWFLAKE_ACCOUNT_URL="https://example.snowflakecomputing.com"

cortex_code_env=(
  --ae "SNOWFLAKE_ACCOUNT=$SNOWFLAKE_ACCOUNT"
  --ae "SNOWFLAKE_USER=$SNOWFLAKE_USER"
  --ae "SNOWFLAKE_ROLE=$SNOWFLAKE_ROLE"
  --ae "SNOWFLAKE_WAREHOUSE=$SNOWFLAKE_WAREHOUSE"
)
```

For PAT authentication, set:

```bash
export SNOWFLAKE_PAT="your-programmatic-access-token"
export CORTEX_PAT="$SNOWFLAKE_PAT"
cortex_code_auth=(--ae 'SNOWFLAKE_PAT=${SNOWFLAKE_PAT}')
```

For OAuth instead, set:

```bash
export CORTEX_PAT="your-oauth-access-token"
export CORTEX_TOKEN_TYPE=OAUTH
cortex_code_auth=(
  --ae "SNOWFLAKE_AUTHENTICATOR=oauth"
  --ae 'SNOWFLAKE_PASSWORD=${CORTEX_PAT}'
)
```

Then run Harbor with the chosen credentials:

```bash
harbor run --config configs/cortex-code.yaml -p tasks/ecommerce \
  -a cortex-code -m your-agent-model \
  --ak 'disallowed_tools=["web_search","web_fetch"]' \
  "${cortex_code_env[@]}" "${cortex_code_auth[@]}"
```

Replace every `your-*` value and the `example` account URL, including the agent model,
**before** running the block. After Harbor finishes, select the new directory
under `jobs/` (configured in `configs/cortex-code.yaml`):

```bash
ls jobs
find jobs/your-job-name -name result.json
```

Each trial's `result.json` holds the Harbor result and verifier rewards;
verifier artifacts include the report and evaluation details. For a report
saved outside Harbor, score it directly with
`uv run avbench score --task ecommerce --report report.md`.

To measure CATC, use the job directory you just found and the same Snowflake
connection. The collecting role also needs read access to Snowflake
`ACCOUNT_USAGE` query and Cortex AI usage views (see [CATC](docs/catc.md)):

```bash
uv run python scripts/collect_snowflake_usage.py jobs/your-job-name \
  --connection bench --output .avbench/catc.json
```

Inspect `.avbench/catc.json` → `trials`: `total_catc` is available only for
complete attribution (`status` `exact` or `estimated`); otherwise inspect
`known_catc_lower_bound` and the component reasons. Account Usage can lag, so
rerun collection later if necessary. Judge calls are excluded from CATC.
The same output reports monetary cost at pinned provider list prices under
`trials[].monetary_cost` (`total_usd`, or `known_usd_lower_bound` when usage is
partial); see [Monetary cost](docs/monetary-cost.md).

## All three tasks

Once the first run works, fetch and load the remaining archives, then run all
tasks with the same agent settings. Source **all three** generated environment
files so Harbor can bind the correct manifest to each task:

```bash
git lfs pull
uv run avbench setup --dataset customer-support --connection bench
uv run avbench setup --dataset travel --connection bench
source .avbench/deployments/ecommerce.env
source .avbench/deployments/customer-support.env
source .avbench/deployments/travel.env
harbor run --config configs/cortex-code.yaml -p tasks \
  -a cortex-code -m your-agent-model \
  --ak 'disallowed_tools=["web_search","web_fetch"]' \
  "${cortex_code_env[@]}" "${cortex_code_auth[@]}"
```

Harbor 0.22 uses `-p tasks` for the local three-task directory. The root
`dataset.toml` is the digest-pinned manifest used for Harbor Hub publication.
Total-token and page-metered AISQL assumptions are in `configs/catc.json`; pass
`--aisql-estimates <path>` to use a different recorded configuration.

Direct scores are written under `.avbench/scores/`. CATC also includes
unweighted four-way token totals when usage attribution is complete.

The archives total about 404 MiB compressed. Agent runtime is capped at two
hours per task. A complete evaluation makes up to 45, 75, or 54 one-shot judge
requests respectively, each capped at 4,096 output tokens. Actual warehouse,
model, and REST costs depend on the user's Snowflake contract and report length;
preflight these costs before a full run.

Changing a model, task, or scoring component can change a result. Report
distributions over repeated trials and record the hashes emitted in `run.json`.

## Benchmark integrity

Harbor uses `network_mode = "public"` so agents can reach Snowflake; this also
permits access to other internet hosts. The example `disallowed_tools` setting
is agent-cooperative and requires Harbor 0.22.0 or later: older Harbor silently
drops it. Pin Harbor 0.22.0 as an integrity requirement, not just for CLI
compatibility. Where possible, restrict egress independently of the agent:

```bash
harbor run ... --allow-agent-host <model-api-host> \
  --allow-agent-host <account>.snowflakecomputing.com
```

The task files contain a training-corpus canary. Published rubric answer keys
make these tasks unsuitable as a held-out leaderboard; see
[limitations](docs/limitations.md).

## Documentation

- [Data loading and deployment contract](docs/deployment.md)
- [Dataset card](docs/dataset-card.md)
- [Evaluation](docs/evaluation.md)
- [CATC](docs/catc.md)
- [Monetary cost](docs/monetary-cost.md)
- [Limitations](docs/limitations.md)
- [Version policy](docs/versions.md)
- [Security](SECURITY.md) and [citation](CITATION.cff)

## License

Copyright (c) Snowflake Inc. All rights reserved.
Licensed under the Apache 2.0 license.
