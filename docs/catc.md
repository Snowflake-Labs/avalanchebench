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

# Capability-adjusted token cost (CATC)

For a first run, follow the [e-commerce walkthrough](../README.md#first-run-e-commerce)
to create a Harbor job, then run the collector against `jobs/<job>` with
`--connection` pointing to **your** Snowflake account. The collecting role
needs access to `SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY`,
`CORTEX_AISQL_USAGE_HISTORY`, and `CORTEX_AI_FUNCTIONS_USAGE_HISTORY` in the
same schema; ask an account administrator to grant access if queries fail.
Pass `--agent-user YOUR_USER` when the collecting user differs from the agent.
Otherwise identity comes from trial configuration or `SNOWFLAKE_USER`. Missing
identity or execution windows, uncovered trajectory query IDs, empty session
history, and procedural/dynamic SQL make coverage partial. Account Usage can
lag: the default `--minimum-ledger-age-seconds 10800` withholds a complete total
until three hours after the agent window ends. Rerun the collector later; only
lower the age bound when independently established ledger coverage warrants it. Output is in `trials[]`, with a
`status` and either `total_catc` or `known_catc_lower_bound` for each trial.

AvalancheBench `0.1.0` publishes this formula:

```text
capability_factor(model) = 10 ** ((arena_rating(model) - arena_rating(anchor)) / 400)

CATC = capability_factor(model) * (
    1.00 * input_tokens
  + 0.10 * cache_read_tokens
  + 1.25 * cache_write_tokens
  + 5.00 * output_tokens
)
```

The pinned source is `lmarena-ai/leaderboard-dataset`, configuration
`text_style_control`, split `latest`, category `overall`, revision
`d25aabda0010d1986a11988e5cb0a748053ce02b`, retrieved 2026-09-17. The anchor
is `llama-3.3-70b-instruct`. Exact ratings and Cortex mappings are in
`src/avalanche_bench/resources/arena_snapshot.json`.

`agent_catc` covers the benchmark agent. `aisql_catc` covers Snowflake AI calls
issued by that agent when their model and token attribution are measurable.
The Harbor collector preserves each model's four-way usage from Cortex Code's
result-event `modelUsage` when available, including auxiliary and unmapped models.
Otherwise it uses detailed aggregate usage; Harbor's collapsed cache total is
an explicit lower-bound fallback.

Agent SQL is attributed by Snowflake session, not only by the trajectory.
Agents can run SQL from Python or spread it over pooled connections, so many
statements never show a query ID in the trace. The collector reads the
agent user's `QUERY_HISTORY` during each trial's Harbor agent window and
assigns a session to a trial when the session:

1. contains a query ID from that trial's trajectory;
2. shares a Cortex Code `request_id` query tag with such a session; or
3. is a Cortex Code session (query tag `app` = `cortex_code_cli`) lying entirely
   inside that trial's agent window and no other trial's.

Run collection over all concurrently executed trials together, preferably with a
user dedicated to benchmark runs. Sessions with AI statements inside an agent
window that cannot be assigned to a single trial are listed in the AISQL reason
and make the component partial. `agent_session_ids` records the assigned
sessions.

`AI_COMPLETE` supplies reported input and output usage and is measured directly.
Most other token-metered Snowflake AI functions supply only total tokens. Their
input/output proportions and reference models are configured in
`configs/catc.json`; cache read and cache write are zero. The current assumptions
use `llama3.3-70b` and these input proportions:

- 99% for `AI_CLASSIFY` and `AI_FILTER`
- 95% for `AI_AGG` and `AI_SENTIMENT`
- 80% for `AI_SUMMARIZE_AGG`
- 90% for `AI_EXTRACT`
- 50% for output-length-preserving functions (`AI_TRANSLATE` and `AI_REDACT`)
- 100% for `AI_EMBED` and `AI_SIMILARITY`

`AI_PARSE_DOCUMENT` is page-metered in
`CORTEX_AI_FUNCTIONS_USAGE_HISTORY`. Each reported page is estimated as 2,000
visual input tokens and 600 extracted-text output tokens with
`claude-sonnet-4-6` as the multimodal reference model. The published
[GPT tile rules](https://developers.openai.com/api/docs/guides/images-vision#tile-based-image-tokenization)
and [Claude patch rules](https://platform.claude.com/docs/en/build-with-claude/vision#resolution-and-token-cost)
put a full rasterized document page at roughly 1,100–2,700 visual tokens,
depending on resolution and detail tier. Meanwhile, 400–500 words of extracted
text is roughly 500–700 output tokens. The rounded 2,000/600 assumptions are
configurable and intentionally sit inside those ranges.

When historical `AI_PARSE_DOCUMENT` calls predate page-level ledger coverage,
the collector estimates pages from successful document rows in Query History.
The fallback uses 1.6467 pages per document, the observed 8,892 / 5,400 ratio
from the six fully page-metered static runs, before applying the same 2,000/600
token conversion.

These configured splits produce an `estimated` CATC, not an exact CATC. The
collector records each query's reported or estimated attribution in
`aisql_query_attribution`, and embeds the effective estimate configuration in
its output for reproducibility.

For a multi-statement tool call, Snowflake can return the final statement's
query ID even when an earlier statement contains the AI function. If that final
statement has no AI function or usage, the collector searches the preceding 20
statements in the same session and query tag and uses the nearest exact function
match. Each physical query is counted once even when session history and the trace
refer to it through different aliases. The attribution preserves both IDs and labels the recovery
`preceding_session_statement`. On mixed-function statements, a usage row named
`default` is assigned only when exactly one traced function lacks a named row;
that function's own configured split is then applied.

`total_catc` is emitted when both components are complete and has status `exact`
or `estimated`. If usage is missing, `known_catc_lower_bound` reports only the
directly measured work and status is `partial_known_lower_bound` or
`unavailable`. Raw usage is preserved for unknown models; an unavailable mapping
never breaks task scoring.

Evaluator/judge calls are outside the metric and can never enter `total_catc`.

The same output also reports `agent_total_tokens`, `aisql_total_tokens`, and
`total_tokens`. These are the unweighted sums of uncached input, cache read,
cache write, and output tokens from the same agent-plus-AISQL boundary. They are
emitted only when the corresponding usage is complete; estimated token
attribution remains labeled by the CATC `status` and component records.
Hidden-model, unattributed, or unconfigured AI operations are marked partial or
unavailable rather than estimated silently. Page-metered operations are only
estimated when the page count is reported by Snowflake and a per-page estimate
is configured.

The Account Usage collector limits each Snowflake ledger query to 300 seconds
by default. Use `--statement-timeout-seconds` to change that bound. A timed-out
ledger produces an explicit partial/unavailable AISQL component so the agent
lower bound is still retained and the collection can be rerun after Account
Usage settles.
