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

# Security Policy

AvalancheBench is a benchmark harness, not a production service. It must not
contain credentials, customer data, or private account locations.

Report vulnerabilities privately through GitHub's **Report a vulnerability**
feature for this repository or by emailing **security@snowflake.com**. Include
the affected path and reproduction steps; do not open a public issue containing
sensitive details.

Running the benchmark requires user-supplied Snowflake credentials. Keep them in
Snowflake connection configuration or environment variables. The generated
deployment manifests contain only physical data identifiers and are safe to
inspect, but they must never contain account URLs, tokens, passwords, or keys.

Use dedicated benchmark identities with access only to the required datasets,
warehouse, and Cortex models. Keep agent and verifier credentials separate and
store verifier credentials outside the agent workspace. An agent can modify
its own container filesystem: verification with credentials unavailable to the
agent must run in a fresh, trusted environment. Report-file validation does not
make an agent-controlled container a security boundary.

Scoring requires a UTF-8 regular report file, at most 16 MiB, with no symbolic
links in its path and no hard links. The verifier snapshots that file once for
grading, hashing, and artifact export. Keep output directories private, and
review reports and logs before sharing them; agent-authored content can contain
sensitive information.

Authenticated Cortex requests reject redirects. Configure the final HTTPS
endpoint directly, keep TLS verification enabled, and independently restrict
network access where possible. Loopback HTTP endpoints are supported for local
tests only.

