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

# Evaluation

Each task publishes a CSV rubric containing its questions, reference answers,
allowed score buckets, and per-question maximum. The evaluator extracts report
sections using format-robust question markers. It accepts ATX headings at levels
one through six, harmless case/spacing/punctuation and bold variants, leading
zeros, standalone `Q1`/`Question 1` markers, and exact normalized rubric-title
headings. Markers in fenced code, block quotes, indented code, and common raw
HTML regions are ignored. A section ends at the next recognized question marker,
not at an unrelated subsection heading.

The default context policy is `section_first_fallback`: a unique high-confidence
section is sent to the judges, while a missing, blank, duplicated, or ambiguous
extraction sends the whole unchanged report with the same single rubric question.
Formatting uncertainty cannot itself create a zero. An empty or whitespace-only
report is the sole no-call zero because it contains no evidence. Set
`AVBENCH_JUDGE_CONTEXT_MODE` to `whole_report` to judge every question against the
full report, or to `robust_section_only` to fail closed when a unique section is
unavailable.

Each question is sent to the pinned committee members:
`claude-opus-4-8`, `openai-gpt-5.4`, and `gemini-3.7-flash`. Requests use
Snowflake Cortex REST, temperature `0`, `stream=false`, and a 4,096-token
output cap. The three judges for one question run concurrently using independent
clients; questions remain sequential. An altered committee is not an official
`0.1.0` score.

The default PAT/OAuth client uses the OpenAI-compatible
`/api/v2/cortex/v1/chat/completions` endpoint with `max_completion_tokens`.
Set `CORTEX_REST_ENDPOINT` to the model-agnostic
`/api/v2/cortex/inference:complete` endpoint to use `max_tokens` instead. Test all three aliases in the chosen account and
endpoint before scoring: model availability and REST routing vary by account.
Judges use `CORTEX_PAT`, `SNOWFLAKE_ACCOUNT_URL`, and `CORTEX_TOKEN_TYPE`
(`OAUTH` or `PROGRAMMATIC_ACCESS_TOKEN`), independently of the agent connection.
No judge inference runs through SQL.

REST requests declare `ANY_REGION` through the cross-region inference header
by default. Set `CORTEX_REST_CROSS_REGION` to a narrower administrator-approved
region list, or to an empty value to omit the header. The account must also
permit the required routing through `CORTEX_ENABLED_CROSS_REGION`.

For **local scoring**, `CORTEX_SNOWFLAKE_CONNECTION_NAME=analytics` uses three
independent named-connector sessions to authenticate REST requests to the
model-agnostic endpoint. The connector reads its credential or token file;
judge inference still uses REST. The named connection is not forwarded into
Harbor because host connection files are unavailable in its verifier container.
Use the documented PAT/OAuth environment settings there. Credentials never
appear in scoring artifacts.

The question score is the median numeric bucket from exactly three votes. The
evaluator emits question, category, point, normalized, completion, and committee
agreement rewards. `evaluation.json` retains justifications; `run.json` retains
content hashes, model/endpoint settings, timestamps, request IDs, latency, and
ordinary token usage.

The Harbor verifier publishes numeric `reward.json` with mode `0644` so a
non-root host can read it before container cleanup. Reports, evaluation details,
and run audits retain private mode `0600`.

Release snapshots identify canonical source using `evaluator_source_tree`, the
Git tree of `src/avalanche_bench` included in the single publication commit.
The evaluator, extractor, and Cortex modules also retain their individual
SHA-256 hashes. This avoids embedding a commit's own hash inside that commit.
`run.json` records the source tree; its legacy `evaluator_source_commit` field
is null unless a commit is explicitly supplied. Older private commit-based
release metadata remains supported.

Retryable transport failures and invalid judge output receive at most two total
attempts per model, with a one-second backoff. Authentication, configuration,
and other non-retryable errors are not retried; scores and report quality never
trigger a retry. Every attempt retains its requested/resolved model (when the
service supplies one), request ID, timing, latency, usage, and error class. An
incomplete committee aborts evaluation. Score outputs are prepared on temporary
paths and published only after the entire evaluation succeeds; `failure.json`
contains the attempt audit without publishing a partial score. Judge usage is
diagnostic only and is explicitly outside CATC.

Each question also records extraction method/confidence, original and normalized
marker, character and byte offsets, span hash/length, duplicate candidates,
fallback reason, final prompt scope, and prompt hash. Raw prompt text is omitted
by default; `AVBENCH_INCLUDE_PRIVATE_PROMPTS=1` includes it in `run.json` for a
private audit bundle. Such output can contain the complete report and must not be
published without the benchmark's privacy and secret-scanning gates.

The PAT user needs `SNOWFLAKE.CORTEX_REST_API_USER` or
`SNOWFLAKE.CORTEX_USER`, plus access to all pinned models. Credentials are read
only from environment variables and never accepted as CLI arguments. The
account must also permit the cross-region routing required to make every pinned
model available; an account administrator controls this with
`CORTEX_ENABLED_CROSS_REGION`.
