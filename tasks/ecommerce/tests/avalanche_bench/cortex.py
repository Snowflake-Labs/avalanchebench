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

"""One-shot client for Snowflake's OpenAI-compatible Cortex REST endpoint."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

ENDPOINT_PATH = "/api/v2/cortex/v1/chat/completions"
INFERENCE_ENDPOINT_PATH = "/api/v2/cortex/inference:complete"
DEFAULT_MODELS = (
    "claude-opus-4-8",
    "openai-gpt-5.4",
    "gemini-3.7-flash",
)
DEFAULT_MAX_TOKENS = 4096
JUDGE_OUTPUT_LIMIT_ENV = "CORTEX_JUDGE_OUTPUT_LIMIT"


class CortexError(RuntimeError):
    """A single Cortex request failed or returned an invalid response."""

    def __init__(
        self,
        message: str,
        *,
        error_class: str = "cortex_error",
        retryable: bool = False,
        http_status: int | None = None,
        request_id: str | None = None,
        latency_ms: float | None = None,
        usage: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_class = error_class
        self.retryable = retryable
        self.http_status = http_status
        self.request_id = request_id
        self.latency_ms = latency_ms
        self.usage = usage or {}


@dataclass(frozen=True)
class CortexResponse:
    text: str
    request_id: str | None
    usage: dict[str, Any]
    latency_ms: float
    resolved_model: str | None = None


def configured_models() -> tuple[str, str, str]:
    raw = os.environ.get("CORTEX_JUDGE_MODELS", ",".join(DEFAULT_MODELS))
    models = tuple(part.strip() for part in raw.split(",") if part.strip())
    if len(models) != 3 or len(set(models)) != 3:
        raise CortexError("CORTEX_JUDGE_MODELS must contain three distinct aliases")
    return models  # type: ignore[return-value]


def _response_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise CortexError(
            "Cortex response must contain exactly one choice",
            error_class="invalid_response",
            retryable=True,
        )
    choice = choices[0]
    if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
        raise CortexError(
            "Cortex response choice has no message",
            error_class="invalid_response",
            retryable=True,
        )
    content = choice["message"].get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    if isinstance(content, list):
        joined = "".join(
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ).strip()
        if joined:
            return joined
    raise CortexError(
        "Cortex response message contains no text",
        error_class="empty_response",
        retryable=True,
    )


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Authentication must only be sent to the configured endpoint.
        return None


def _open_request(request: urllib.request.Request, *, timeout: float):
    return urllib.request.build_opener(_RejectRedirects()).open(
        request, timeout=timeout
    )


class CortexClient:
    """Send one request per call using only immutable, thread-safe state.

    Each request creates its own opener with redirects disabled, and this
    class does not hold a shared session or mutate credentials. A client is safe
    for concurrent calls; production scoring still creates one client per judge
    to make connection ownership explicit.
    """

    thread_safe = True
    endpoint_path = ENDPOINT_PATH

    def __init__(
        self,
        *,
        account_url: str | None = None,
        pat: str | None = None,
        endpoint: str | None = None,
        timeout_seconds: float | None = None,
        max_tokens: int | None = None,
        cross_region: str | None = None,
    ) -> None:
        account_url = account_url or os.environ.get("SNOWFLAKE_ACCOUNT_URL")
        endpoint = endpoint or os.environ.get("CORTEX_REST_ENDPOINT") or None
        if not endpoint:
            if not account_url:
                raise CortexError(
                    "SNOWFLAKE_ACCOUNT_URL is required",
                    error_class="configuration_error",
                )
            endpoint = account_url.rstrip("/") + ENDPOINT_PATH
        parsed = urlsplit(endpoint)
        if not parsed.hostname or not (
            parsed.scheme == "https"
            or (
                parsed.scheme == "http"
                and parsed.hostname in {"127.0.0.1", "::1", "localhost"}
            )
        ):
            raise CortexError(
                "Cortex endpoint must use HTTPS", error_class="configuration_error"
            )
        self.endpoint = endpoint
        self.endpoint_path = parsed.path
        self.pat = (
            pat or os.environ.get("CORTEX_PAT") or os.environ.get("SNOWFLAKE_PAT")
        )
        if not self.pat:
            raise CortexError("CORTEX_PAT is required", error_class="authentication")
        self.timeout_seconds = timeout_seconds or float(
            os.environ.get("CORTEX_REST_TIMEOUT_SECONDS", "180")
        )
        self.max_tokens = max_tokens or int(
            os.environ.get(JUDGE_OUTPUT_LIMIT_ENV, str(DEFAULT_MAX_TOKENS))
        )
        if self.timeout_seconds <= 0 or self.max_tokens <= 0:
            raise CortexError(
                "timeout and max_tokens must be positive",
                error_class="configuration_error",
            )
        self.token_type = os.environ.get(
            "CORTEX_TOKEN_TYPE", "PROGRAMMATIC_ACCESS_TOKEN"
        )
        self.cross_region = (
            cross_region
            if cross_region is not None
            else os.environ.get("CORTEX_REST_CROSS_REGION", "ANY_REGION")
        ).strip()

    def complete(self, model: str, prompt: str) -> CortexResponse:
        body = json.dumps(
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                (
                    "max_tokens"
                    if self.endpoint_path == INFERENCE_ENDPOINT_PATH
                    else "max_completion_tokens"
                ): self.max_tokens,
                "stream": False,
            }
        ).encode()
        headers = {
            "Authorization": f"Bearer {self.pat}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Snowflake-Authorization-Token-Type": self.token_type,
        }
        if self.cross_region:
            headers["sf-ml-enabled-cross-regions"] = self.cross_region
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            method="POST",
            headers=headers,
        )
        started = time.monotonic()
        try:
            with _open_request(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
                headers = response.headers
                status = response.status
        except urllib.error.HTTPError as exc:
            latency_ms = round((time.monotonic() - started) * 1000, 3)
            detail = exc.read(500).decode("utf-8", errors="replace").strip()
            request_id = exc.headers.get("x-snowflake-request-id") or exc.headers.get(
                "x-request-id"
            )
            raise CortexError(
                f"Cortex HTTP {exc.code}: {detail}",
                error_class="http_error",
                retryable=exc.code in {408, 409, 429, 500, 502, 503, 504},
                http_status=exc.code,
                request_id=request_id,
                latency_ms=latency_ms,
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise CortexError(
                f"Cortex request failed: {type(exc).__name__}: {exc}",
                error_class="transport_error",
                retryable=True,
                latency_ms=round((time.monotonic() - started) * 1000, 3),
            ) from exc
        latency_ms = round((time.monotonic() - started) * 1000, 3)
        if status != 200:
            raise CortexError(
                f"Cortex HTTP {status}",
                error_class="http_error",
                retryable=status in {408, 409, 429, 500, 502, 503, 504},
                http_status=status,
            )
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CortexError(
                "Cortex response is not valid JSON",
                error_class="invalid_json_response",
                retryable=True,
            ) from exc
        if not isinstance(payload, dict):
            raise CortexError(
                "Cortex response must be a JSON object",
                error_class="invalid_response",
                retryable=True,
            )
        usage = payload.get("usage", {})
        if not isinstance(usage, dict):
            raise CortexError(
                "Cortex response usage must be a JSON object",
                error_class="invalid_response",
                retryable=True,
            )
        request_id = (
            headers.get("x-snowflake-request-id")
            or headers.get("x-request-id")
            or payload.get("request_id")
            or payload.get("id")
        )
        return CortexResponse(
            text=_response_text(payload),
            request_id=str(request_id) if request_id is not None else None,
            usage=usage,
            latency_ms=latency_ms,
            resolved_model=(
                str(payload.get("resolved_model") or payload.get("model"))
                if payload.get("resolved_model") or payload.get("model")
                else None
            ),
        )


class SnowflakeCortexClient:
    """Model-agnostic Cortex REST client backed by a named connector session."""

    thread_safe = False
    endpoint_path = INFERENCE_ENDPOINT_PATH
    token_type = "SESSION_TOKEN"

    def __init__(
        self,
        *,
        connection_name: str,
        timeout_seconds: float | None = None,
        max_tokens: int | None = None,
        cross_region: str | None = None,
    ) -> None:
        import snowflake.connector

        self.connection_name = connection_name
        self.connection = snowflake.connector.connect(connection_name=connection_name)
        host = str(self.connection.host).lower().replace("_", "-")
        self.endpoint = f"https://{host}{self.endpoint_path}"
        self.timeout_seconds = timeout_seconds or float(
            os.environ.get("CORTEX_REST_TIMEOUT_SECONDS", "180")
        )
        self.max_tokens = max_tokens or int(
            os.environ.get(JUDGE_OUTPUT_LIMIT_ENV, str(DEFAULT_MAX_TOKENS))
        )
        self.cross_region = (
            cross_region
            if cross_region is not None
            else os.environ.get("CORTEX_REST_CROSS_REGION", "ANY_REGION")
        ).strip()
        if self.timeout_seconds <= 0 or self.max_tokens <= 0:
            raise CortexError(
                "timeout and max_tokens must be positive",
                error_class="configuration_error",
            )

    def close(self) -> None:
        self.connection.close()

    def complete(self, model: str, prompt: str) -> CortexResponse:
        token = self.connection.rest.token
        if not token:
            raise CortexError(
                "Snowflake connector session token is empty",
                error_class="authentication",
            )
        body = json.dumps(
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": self.max_tokens,
                "stream": False,
            }
        ).encode()
        headers = {
            "Authorization": f'Snowflake Token="{token}"',
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.cross_region:
            headers["sf-ml-enabled-cross-regions"] = self.cross_region
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            method="POST",
            headers=headers,
        )
        started = time.monotonic()
        try:
            with _open_request(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
                response_headers = response.headers
                status = response.status
        except urllib.error.HTTPError as exc:
            latency_ms = round((time.monotonic() - started) * 1000, 3)
            detail = exc.read(500).decode("utf-8", errors="replace").strip()
            request_id = exc.headers.get("x-snowflake-request-id") or exc.headers.get(
                "x-request-id"
            )
            raise CortexError(
                f"Cortex HTTP {exc.code}: {detail}",
                error_class="http_error",
                retryable=exc.code in {408, 409, 429, 500, 502, 503, 504},
                http_status=exc.code,
                request_id=request_id,
                latency_ms=latency_ms,
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise CortexError(
                f"Cortex request failed: {type(exc).__name__}: {exc}",
                error_class="transport_error",
                retryable=True,
                latency_ms=round((time.monotonic() - started) * 1000, 3),
            ) from exc
        latency_ms = round((time.monotonic() - started) * 1000, 3)
        if status != 200:
            raise CortexError(
                f"Cortex HTTP {status}",
                error_class="http_error",
                retryable=status in {408, 409, 429, 500, 502, 503, 504},
                http_status=status,
                latency_ms=latency_ms,
            )
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CortexError(
                "Cortex response is not valid JSON",
                error_class="invalid_json_response",
                retryable=True,
                latency_ms=latency_ms,
            ) from exc
        if not isinstance(payload, dict):
            raise CortexError(
                "Cortex response must be a JSON object",
                error_class="invalid_response",
                retryable=True,
                latency_ms=latency_ms,
            )
        usage = payload.get("usage", {})
        if not isinstance(usage, dict):
            raise CortexError(
                "Cortex response usage must be a JSON object",
                error_class="invalid_response",
                retryable=True,
                latency_ms=latency_ms,
            )
        request_id = (
            response_headers.get("x-snowflake-request-id")
            or response_headers.get("x-request-id")
            or payload.get("request_id")
            or payload.get("id")
        )
        return CortexResponse(
            text=_response_text(payload),
            request_id=str(request_id) if request_id is not None else None,
            usage=usage,
            latency_ms=latency_ms,
            resolved_model=(
                str(payload.get("resolved_model") or payload.get("model"))
                if payload.get("resolved_model") or payload.get("model")
                else None
            ),
        )


def configured_client_factory() -> type[CortexClient] | Callable[[], Any]:
    """Return the configured independent-client constructor."""

    connection_name = os.environ.get("CORTEX_SNOWFLAKE_CONNECTION_NAME", "").strip()
    if connection_name:
        return lambda: SnowflakeCortexClient(connection_name=connection_name)
    return CortexClient
