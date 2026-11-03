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

# Contributing

Open an issue describing the problem or proposal, then submit a focused pull
request. Report sensitive security issues through the channels in SECURITY.md.
Keep credentials, account locations, generated reports, and trial logs out of
issues, patches, and Git history.

Include the Snowflake copyright and Apache-2.0 header in new source files,
using the current year and the file format's comment syntax. Use an adjacent
`.license` file for JSON, CSV, and binary files. Preserve third-party licenses
and attribution, including the LM Arena snapshot's CC BY 4.0 license.

Install Python 3.12 and run `uv sync --locked`. Before submitting a change, run:

```bash
uv run ruff format --check .
uv run ruff check .
uv run pytest
uv run scripts/sync_task_runtime.py --check
```

After changing canonical runtime code, run `uv run scripts/sync_task_runtime.py`
and include the synchronized task copies. Follow docs/versions.md for changes
to released data, prompts, scoring, or dependencies. Refresh release provenance
and task digests with scripts/refresh_release_metadata.py. To include source and
metadata changes in one commit, stage the reviewed source and run
`uv run scripts/refresh_release_metadata.py --staged`, then stage the refreshed
metadata and commit. Do not invent Git tree identifiers; the script derives
them from the staged canonical source.

The full release scan requires materialized Git LFS archives and Tesseract with
English language data. On Ubuntu install `tesseract-ocr tesseract-ocr-eng`.
