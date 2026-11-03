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

# AvalancheBench 0.1.0 dataset card

## Purpose and construction

Three frozen, fully synthetic enterprise datasets test whether an agent can
turn heterogeneous evidence into a decision-ready analytical report. Each was
generated from a seeded latent world specification defining entities, their
relationships, and behavioral and statistical parameters. Tabular records
were sampled programmatically; free-text fields (including reviews, tickets,
comments, documents, and captions) and travel images were produced using
generative models. No Snowflake customer or third-party proprietary data is
included. The generator prompts and oracle mappings are not published.

## Dataset inventory

| Dataset | Tables (rows) | Other assets | Grain | Archive SHA-256 |
|---|---|---|---|---|
| E-commerce | products (20), reviews (20,000), sales (28,221) | — | one review or sales line per row | `5a2096c860c5b7d4cd105c52df455bb7adb37cb8d613038c3b5a3b0f31dc068a` |
| Customer support | tickets (5,000), ticket_comments (19,653) | 900 knowledge-base documents: 486 PDF, 252 DOCX, 162 PPTX | one ticket or comment per row; documents support citations | `7facd266978106171c08fc470284f8011e4ef306fa843f7e04fd4de13ebdd312` |
| Travel | destinations (8), campaigns (8), images (200), visitors (5,000), interactions (654,282), comments (8,457) | 200 JPG images | one ad impression per interaction row | `9d797314cddf2208d3e680a2e20703d069315f9340a8323ff8eeaf1e93c67c0a` |

Load any `0.1.0` archive with `uv run avbench setup --dataset <name>
--connection <name>`, using `ecommerce`, `customer-support`, or `travel` as
`<name>`; checksums are also recorded in `data/manifest.json`.

## Uses and limitations

Intended for benchmarking data-to-report agents with `avbench` and Harbor.
Do not use these data as a proxy for actual retail, support, travel, or any
named organization's activity, or claim that scores measure performance on a
gaming-resistant held-out leaderboard: rubric keys and reference answers are
published. LLM-judge scores vary across runs, and travel interactions are
impressions, not unique visitor/image/month combinations. See
[limitations](limitations.md).

## License and provenance

The synthetic release datasets are covered by Apache-2.0 (`LICENSE` and
`NOTICE`). Free text, documents, and the 200 travel images are synthetic;
The release owner retains dataset and image rights approval; the final
repository remains subject to legal review. No customer or third-party proprietary data is included.

The de-branded e-commerce and customer-support tables have pinned Snowflake
`HASH_AGG` fingerprints measured by loading their CSVs into temporary tables
using the reference loader's schema and CSV options. The release gate verifies
them again in fresh isolated schemas.
