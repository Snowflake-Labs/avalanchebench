# Copyright 2026 Snowflake Inc.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from avalanche_bench.cortex import CortexClient, CortexError, SnowflakeCortexClient
from avalanche_bench.evaluation import (
    MAX_REPORT_BYTES,
    EvaluationError,
    read_report,
    write_bytes_atomically,
    write_outputs_atomically,
)


@pytest.mark.parametrize("kind", ["symlink", "parent_symlink", "hardlink", "fifo"])
def test_untrusted_report_links_and_special_files_are_rejected(tmp_path, kind):
    private = tmp_path / "private.md"
    private.write_text("fixture private content")
    report = tmp_path / "report.md"
    if kind == "symlink":
        report.symlink_to(private)
    elif kind == "parent_symlink":
        directory = tmp_path / "reports"
        directory.mkdir()
        (directory / "report.md").write_text("fixture report")
        parent = tmp_path / "linked-reports"
        parent.symlink_to(directory, target_is_directory=True)
        report = parent / "report.md"
    elif kind == "hardlink":
        os.link(private, report)
    else:
        os.mkfifo(report)
    with pytest.raises(EvaluationError, match="regular file"):
        read_report(report)


def test_report_size_limit_is_checked_before_reading(tmp_path):
    report = tmp_path / "report.md"
    with report.open("wb") as handle:
        handle.truncate(MAX_REPORT_BYTES + 1)
    with pytest.raises(EvaluationError, match="size limit"):
        read_report(report)


def test_report_snapshot_is_not_redirected_by_path_replacement(tmp_path, monkeypatch):
    report = tmp_path / "report.md"
    report.write_bytes(b"fixture report\r\n")
    private = tmp_path / "private.md"
    private.write_text("fixture private content")
    original_open = os.open

    def replace_after_open(path, flags, *args, **kwargs):
        descriptor = original_open(path, flags, *args, **kwargs)
        if path == "report.md":
            report.rename(tmp_path / "original-report.md")
            report.symlink_to(private)
        return descriptor

    monkeypatch.setattr(os, "open", replace_after_open)
    assert read_report(report) == b"fixture report\r\n"


@pytest.mark.parametrize("multiple", [False, True])
def test_output_staging_does_not_follow_a_predictable_symlink(tmp_path, multiple):
    output = tmp_path / "output.json"
    victim = tmp_path / "private.txt"
    victim.write_text("unchanged fixture")
    predictable = tmp_path / f".{output.name}.{os.getpid()}.tmp"
    predictable.symlink_to(victim)
    if multiple:
        write_outputs_atomically([(output, {"ok": True})])
    else:
        write_bytes_atomically(output, b'{"ok": true}\n')
    assert victim.read_text() == "unchanged fixture"
    assert predictable.is_symlink()
    expected = b'{\n  "ok": true\n}\n' if multiple else b'{"ok": true}\n'
    assert output.read_bytes() == expected
    assert output.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("client_kind", ["pat", "session"])
def test_authenticated_cortex_requests_do_not_follow_redirects(
    status, client_kind, monkeypatch
):
    destination_calls = []

    class Destination(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            destination_calls.append(self.path)
            self.send_response(200)
            self.end_headers()

        do_POST = do_GET

        def log_message(self, _format, *_args):
            pass

    destination = ThreadingHTTPServer(("127.0.0.1", 0), Destination)

    class Redirect(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.send_response(status)
            self.send_header(
                "Location", f"http://127.0.0.1:{destination.server_port}/destination"
            )
            self.end_headers()

        def log_message(self, _format, *_args):
            pass

    redirect = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    servers = [destination, redirect]
    threads = [
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        for server in servers
    ]
    for thread in threads:
        thread.start()
    endpoint = f"http://127.0.0.1:{redirect.server_port}/start"
    if client_kind == "pat":
        client = CortexClient(endpoint=endpoint, pat="fixture-token")
    else:

        class Rest:
            token = "fixture-token"

        class Connection:
            host = "example.snowflakecomputing.com"
            rest = Rest()

        monkeypatch.setattr("snowflake.connector.connect", lambda **_: Connection())
        client = SnowflakeCortexClient(connection_name="fixture")
        client.endpoint = endpoint
    try:
        with pytest.raises(CortexError, match=f"HTTP {status}") as error:
            client.complete("fixture-model", "fixture prompt")
        assert error.value.http_status == status
        assert not error.value.retryable
        assert destination_calls == []
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join()
