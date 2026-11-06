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

# Version policy

All public datasets, tasks, and evaluation components in the initial release are
version `0.1.0`. Public paths and identifiers remain clean and stable; the Git
tag, root archive manifest, task package metadata, and emitted run hashes carry
the exact release identity.

Any change to dataset bytes, a task prompt, rubric, reference answer, scoring logic,
judge committee, aggregation, CATC snapshot/mapping, or container creates a new
release version and new task/archive digests. Published tags and Git LFS objects
are immutable and are never replaced in place.

Operational fixes that preserve data and scoring may retain the benchmark
version, with refreshed source hashes and task digests identifying the patch.
