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

import io
import json
import urllib.error

import pytest

from avalanche_bench.cortex import (
    ENDPOINT_PATH,
    INFERENCE_ENDPOINT_PATH,
    CortexClient,
    CortexError,
    SnowflakeCortexClient,
    configured_client_factory,
)


class Response:
    status = 200
    headers = {"x-snowflake-request-id": "request-id"}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self) -> bytes:
        return json.dumps(
            {
                "choices": [
                    {"message": {"content": '{"bucket":"top","justification":"ok"}'}}
                ],
                "usage": {"input_tokens": 12, "output_tokens": 4},
            }
        ).encode()


@pytest.mark.parametrize(
    "endpoint",
    ["http://127.0.0.1.attacker.example/api", "http://127.0.0.1evil.example/api"],
)
def test_non_loopback_http_endpoint_is_rejected(endpoint: str) -> None:
    with pytest.raises(CortexError, match="HTTPS"):
        CortexClient(endpoint=endpoint, pat="test-pat")


def test_loopback_http_endpoint_is_accepted() -> None:
    assert CortexClient(endpoint="http://127.0.0.1:8080/x", pat="test-pat").endpoint


def test_openai_compatible_request_has_fixed_parameters(monkeypatch) -> None:
    requests = []

    def urlopen(request, timeout):
        requests.append((request, timeout))
        return Response()

    monkeypatch.setattr("avalanche_bench.cortex._open_request", urlopen)
    monkeypatch.setenv("CORTEX_REST_ENDPOINT", "")
    client = CortexClient(
        account_url="https://example.snowflakecomputing.com",
        pat="test-pat",
        timeout_seconds=7,
        max_tokens=123,
    )
    response = client.complete("model", "prompt")
    assert len(requests) == 1
    request, timeout = requests[0]
    assert request.full_url.endswith(ENDPOINT_PATH)
    assert timeout == 7
    body = json.loads(request.data)
    assert body == {
        "model": "model",
        "messages": [{"role": "user", "content": "prompt"}],
        "temperature": 0,
        "max_completion_tokens": 123,
        "stream": False,
    }
    assert request.headers["Authorization"] == "Bearer test-pat"
    assert request.headers["Sf-ml-enabled-cross-regions"] == "ANY_REGION"
    assert response.request_id == "request-id"


def test_cross_region_header_can_be_narrowed_or_omitted(monkeypatch) -> None:
    requests = []

    def urlopen(request, timeout):
        assert timeout > 0
        requests.append(request)
        return Response()

    monkeypatch.setattr("avalanche_bench.cortex._open_request", urlopen)
    CortexClient(
        endpoint="https://example.test", pat="test-pat", cross_region="GCP_US"
    ).complete("model", "prompt")
    CortexClient(
        endpoint="https://example.test", pat="test-pat", cross_region=""
    ).complete("model", "prompt")
    assert requests[0].headers["Sf-ml-enabled-cross-regions"] == "GCP_US"
    assert "Sf-ml-enabled-cross-regions" not in requests[1].headers


def test_pat_inference_endpoint_uses_model_agnostic_token_limit(monkeypatch) -> None:
    requests = []

    def urlopen(request, timeout):
        requests.append(request)
        return Response()

    monkeypatch.setattr("avalanche_bench.cortex._open_request", urlopen)
    client = CortexClient(
        endpoint="https://example.snowflakecomputing.com" + INFERENCE_ENDPOINT_PATH,
        pat="test-pat",
        max_tokens=123,
    )
    client.complete("model", "prompt")
    body = json.loads(requests[0].data)
    assert body["max_tokens"] == 123
    assert "max_completion_tokens" not in body


def test_http_failure_is_not_retried(monkeypatch) -> None:
    calls = 0

    def fail(_request, timeout):
        nonlocal calls
        calls += 1
        raise urllib.error.HTTPError(
            "https://example.test",
            429,
            "rate limited",
            {},
            io.BytesIO(b"rate limited"),
        )

    monkeypatch.setattr("avalanche_bench.cortex._open_request", fail)
    client = CortexClient(endpoint="https://example.test", pat="test-pat")
    with pytest.raises(CortexError, match="HTTP 429"):
        client.complete("model", "prompt")
    assert calls == 1


def test_timeout_failure_is_not_retried(monkeypatch) -> None:
    calls = 0

    def fail(_request, timeout):
        nonlocal calls
        calls += 1
        raise TimeoutError("fixture timeout")

    monkeypatch.setattr("avalanche_bench.cortex._open_request", fail)
    client = CortexClient(endpoint="https://example.test", pat="test-pat")
    with pytest.raises(CortexError, match="TimeoutError"):
        client.complete("model", "prompt")
    assert calls == 1


def test_public_output_limit_environment_name(monkeypatch) -> None:
    monkeypatch.setenv("CORTEX_JUDGE_OUTPUT_LIMIT", "321")
    client = CortexClient(endpoint="https://example.test", pat="test-pat")
    assert client.max_tokens == 321


def test_named_connection_uses_model_agnostic_endpoint(monkeypatch) -> None:
    requests = []

    class Rest:
        token = "session-token"

    class Connection:
        host = "ORG_ACCOUNT_AWS_US_WEST_2.snowflakecomputing.com"
        rest = Rest()
        closed = False

        def close(self) -> None:
            self.closed = True

    connection = Connection()
    monkeypatch.setattr("snowflake.connector.connect", lambda **_kwargs: connection)

    def urlopen(request, timeout):
        assert timeout > 0
        requests.append(request)
        return Response()

    monkeypatch.setattr("avalanche_bench.cortex._open_request", urlopen)
    client = SnowflakeCortexClient(connection_name="analytics")
    assert client.token_type == "SESSION_TOKEN"
    response = client.complete("model", "prompt")
    assert requests[0].full_url.endswith(INFERENCE_ENDPOINT_PATH)
    assert "org-account-aws-us-west-2" in requests[0].full_url
    assert requests[0].headers["Authorization"] == ('Snowflake Token="session-token"')
    body = json.loads(requests[0].data)
    assert body["max_tokens"] == 4096
    assert "max_completion_tokens" not in body
    assert response.request_id == "request-id"
    client.close()
    assert connection.closed is True


def test_configured_factory_selects_named_connection(monkeypatch) -> None:
    monkeypatch.delenv("CORTEX_SNOWFLAKE_CONNECTION_NAME", raising=False)
    assert configured_client_factory() is CortexClient
    monkeypatch.setenv("CORTEX_SNOWFLAKE_CONNECTION_NAME", "analytics")
    assert callable(configured_client_factory())
    assert configured_client_factory() is not CortexClient
