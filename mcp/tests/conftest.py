"""Test doubles for cag-mcp.

A fake cag-api built on httpx.MockTransport stands in for the real HTTP service,
mirroring the endpoints cag_mcp.client calls. The ``fake_api`` fixture patches
cag_mcp.server._client so the tool functions (tested directly, not over stdio)
talk to the fake. This mirrors the spirit of api/tests/conftest.py: a small,
scriptable stand-in that exposes exactly the surface under test.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from cag_mcp import server
from cag_mcp.client import CagClient


class FakeCagApi:
    """Scriptable fake of cag-api over httpx.MockTransport.

    Routes are the ones cag_mcp.client uses. Per-route behaviour can be
    overridden by assigning a handler to ``responses[(method, path)]`` that
    returns an ``httpx.Response``; otherwise sensible defaults apply.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.documents: list[dict] = []
        self.answer = "The safety limit is 8 A continuous."
        # Default /verify verdict (overridable per test).
        self.verdict = "supported"
        self.quote = "peak at 12 A for 10 s"
        self.conditions = ""
        self.quote_grounded: bool | None = True
        self.match_ratio = 1.0
        self.grounding_method = "exact"
        # (method, path) -> handler(request) -> httpx.Response
        self.responses: dict[tuple[str, str], Callable[[httpx.Request], httpx.Response]] = {}

    # -- transport ----------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.path)
        if key in self.responses:
            return self.responses[key](request)
        handler = {
            ("GET", "/documents"): self._list_documents,
            ("POST", "/query"): self._query,
            ("POST", "/verify"): self._verify,
            ("POST", "/documents/text"): self._ingest_text,
            ("POST", "/documents"): self._ingest_file,
        }.get(key)
        if handler is None:
            return httpx.Response(404, json={"detail": f"no route {key}"})
        return handler(request)

    # -- default route behaviour -------------------------------------------

    def _list_documents(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"documents": self.documents})

    def _query(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "answer": self.answer,
                "document": {"id": body.get("document_id") or 7, "file_name": "manual.pdf",
                             "n_tokens": 28400},
                "duration_ms": 640,
                "timings": {
                    "prompt_tokens_evaluated": 43,
                    "prompt_tokens_from_cache": 28400,
                    "answer_tokens": 96,
                    "cache_source": "memory",
                },
            },
        )

    def _verify(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "claim": body.get("claim", ""),
                "verdict": self.verdict,
                "quote": self.quote,
                "conditions": self.conditions,
                "quote_grounded": self.quote_grounded,
                "match_ratio": self.match_ratio,
                "grounding_method": self.grounding_method,
                "document": {"id": body.get("document_id") or 7, "file_name": "manual.pdf",
                             "n_tokens": 28400},
                "duration_ms": 210,
                "timings": {"prompt_tokens_evaluated": 20, "prompt_tokens_from_cache": 28400,
                            "answer_tokens": 30, "cache_source": "memory"},
            },
        )

    def _ingest_text(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(
            201,
            json={"id": 1, "file_name": body.get("file_name", "inline.txt"),
                  "status": "cached", "n_tokens": 1234, "deduplicated": False},
        )

    def _ingest_file(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={"id": 2, "file_name": "manual.pdf", "status": "cached",
                  "n_tokens": 28400, "deduplicated": False},
        )

    # -- helpers for tests --------------------------------------------------

    def set_response(self, method: str, path: str, response: httpx.Response) -> None:
        self.responses[(method, path)] = lambda _req: response

    def fail_connection(self, method: str, path: str) -> None:
        def _boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        self.responses[(method, path)] = _boom


@pytest.fixture
def fake_api(monkeypatch) -> FakeCagApi:
    api = FakeCagApi()

    def _client() -> CagClient:
        client = CagClient(server.CAG_API_URL)
        client._client = httpx.Client(base_url=server.CAG_API_URL, transport=api.transport())
        return client

    monkeypatch.setattr(server, "_client", _client)
    return api
