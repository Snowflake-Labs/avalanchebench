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

# Monetary cost

AvalancheBench reports monetary cost alongside [CATC](catc.md). Both measures
price the same agent-plus-AISQL token usage within the same boundary:

```text
cost_usd = sum over usage records of
    ( input_price      * input_tokens
    + cache_read_price * cache_read_tokens
    + cache_write_price * cache_write_tokens
    + output_price     * output_tokens ) / 1,000,000
```

Prices are third-party list prices in USD per million tokens, pinned in
`src/avalanche_bench/resources/provider_prices.json` and retrieved on
2026-10-29:

- Anthropic models use [Anthropic's pricing page](https://platform.claude.com/docs/en/about-claude/pricing)
  (global endpoint, 5-minute cache writes).
- OpenAI models use [OpenAI's pricing page](https://developers.openai.com/api/docs/pricing)
  (standard tier, 30-minute cache writes).
- Open-weight Llama and Mistral models use the public
  [OpenRouter models API](https://openrouter.ai/api/v1/models).

Snowflake credit prices and contract discounts are not used, so the result is a
provider-list-price estimate, not a Snowflake bill.

## Collection

The [CATC collector](catc.md) writes monetary cost for every trial under
`trials[].monetary_cost`, and the price snapshot source under `price_snapshot`:

```bash
uv run python scripts/collect_snowflake_usage.py jobs/your-job-name \
  --connection bench --output .avbench/catc.json
```

`total_usd` is emitted only when both the agent and AISQL usage are complete.
Otherwise `known_usd_lower_bound` reports the priced work and `status` is
`partial_known_lower_bound` or `unavailable`, exactly as for CATC. Pass
`--prices <path>` to use a different price snapshot with the same schema.

To price usage files directly, use the same files as `avbench catc`:

```bash
uv run avbench cost --agent-usage agent.json --aisql-usage aisql.json \
  --output cost.json
```

## Pricing rules

- AISQL calls are priced on the model that ran them, as reported by Snowflake
  or named in the `AI_COMPLETE` call. CATC instead weights them by the model's
  Arena capability factor.
- Total-token and page-metered functions use the reference models and
  token splits in `configs/catc.json`, because Snowflake does not disclose
  the model behind them: task functions such as `AI_CLASSIFY` and `AI_AGG`
  are priced as `llama3.3-70b`, and each `AI_PARSE_DOCUMENT` page as 2,000
  input and 600 output tokens of `claude-sonnet-4-6`. These costs are
  labeled `estimated`.
- A model without a price, or tokens of a type the provider does not price
  (for example, cache reads on a model with no cache price), make the
  component partial instead of being priced silently.
- `openai-gpt-5.6-sol` prompts over 272k tokens cost 2x input and 1.5x output.
  Session-level usage cannot resolve the tier, so the base tier is used.
  OpenAI marks this model's price as promotional through at least 2026-11-21.
- `pixtral-large` is not listed on OpenRouter and is assumed to cost the same as
  `mistral-large2`.
- Evaluator/judge calls are excluded, as for CATC.

Prices change over time. Report the `price_snapshot` with every result, and
reprice archived usage with `--prices` rather than comparing costs computed at
different dates.
