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

# Limitations

- The datasets are synthetic and simplify real enterprise data, policy,
  governance, and operational access controls.
- A three-model LLM committee remains nondeterministic even at temperature zero;
  scores are estimates, not byte-reproducible facts.
- Rubric publication enables reproducibility but makes these tasks unsuitable as
  gaming-resistant held-out leaderboard items.
- The reference loader and documented execution environment target Snowflake.
  The deployment contract is backend-neutral, but `0.1.0` ships no first-party
  loader or Harbor adapter for another backend.
- Travel interactions are impressions, so a visitor may see the same image more
  than once in a month. Analyses must not treat the visitor/image/month tuple as
  unique.
- CATC depends on a historical Arena snapshot. Total-only AISQL functions use
  configurable input/output and reference-model estimates; hidden, page-metered,
  unattributed, unconfigured, or unmapped operations remain partial or
  unavailable.
- Monetary cost uses list prices pinned on 2026-10-29, not Snowflake credits
  or negotiated rates. Total-only and page-metered AISQL functions are priced on
  the same reference-model estimates as CATC.
- Runtime and cost vary with the agent, model availability, warehouse, and
  report length. The published paper should report repeated trials and exact
  release hashes rather than promise deterministic reproduction.
