"""Thin httpx wrapper around cag-api.

Everything this MCP server knows about the cag-api HTTP surface lives here:
the /query, /documents, /documents/text and GET /documents endpoints. Error
mapping mirrors the style of api/app/llama.py — one place turns transport
failures and 4xx/5xx responses into typed exceptions, and the server layer
turns those into agent-facing prose.
"""

from __future__ import annotations

import httpx

# cag-api's /query timeout is 10 min for CPU worst cases and warming (self-heal)
# can take the full hour; give the client head-room over both.
DEFAULT_TIMEOUT_S = 3600.0


class CagApiError(RuntimeError):
    """cag-api returned an HTTP error response (status >= 400).

    Carries the status code and the server's ``detail`` string (verbatim when
    the body was JSON) so the caller can craft a per-status message.
    """

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"cag-api returned {status_code}: {detail}")


class CagApiUnreachable(RuntimeError):
    """cag-api could not be reached at all (connection refused, DNS, timeout)."""

    def __init__(self, url: str, cause: Exception) -> None:
        self.url = url
        super().__init__(f"cag-api unreachable at {url}: {cause}")


class CagClient:
    def __init__(self, base_url: str, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout_s)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> CagClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- internals ---------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: object) -> dict:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise CagApiUnreachable(self.base_url, exc) from exc
        if response.status_code >= 400:
            raise CagApiError(response.status_code, _detail(response))
        return response.json()

    # --- endpoints ---------------------------------------------------------

    def list_documents(self) -> list[dict]:
        return self._request("GET", "/documents").get("documents", [])

    def query(
        self,
        question: str,
        *,
        document_id: int | None = None,
        max_tokens: int = 1024,
    ) -> dict:
        payload: dict[str, object] = {"question": question, "max_tokens": max_tokens}
        if document_id is not None:
            payload["document_id"] = document_id
        return self._request("POST", "/query", json=payload)

    def ingest_text(self, file_name: str, text: str) -> dict:
        return self._request(
            "POST", "/documents/text", json={"file_name": file_name, "text": text}
        )

    def ingest_file(self, file_name: str, data: bytes, *, content_type: str | None = None) -> dict:
        files = {"file": (file_name, data, content_type or "application/octet-stream")}
        return self._request("POST", "/documents", files=files)


def _detail(response: httpx.Response) -> str:
    """The server's ``detail`` string when the body is JSON, else raw text.

    cag-api reports 413/415 (and friends) as ``{"detail": "..."}``; surfacing
    that field verbatim is part of the tool contract.
    """
    try:
        body = response.json()
    except ValueError:
        return response.text[:1000]
    if isinstance(body, dict) and "detail" in body:
        return str(body["detail"])
    return str(body)[:1000]
