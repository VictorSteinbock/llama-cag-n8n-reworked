"""Thin client for the llama-server HTTP API.

Everything the system knows about llama-server lives here: the OpenAI-compatible
chat endpoint, /tokenize, and the slot save/restore endpoints behind
--slot-save-path. If upstream changes the (experimental) slot API, this is the
only file that needs to follow.
"""

import logging

import httpx

logger = logging.getLogger(__name__)


class LlamaError(RuntimeError):
    """llama-server unreachable or returned an error."""


class LlamaClient:
    def __init__(
        self,
        base_url: str,
        *,
        query_timeout_s: float = 600.0,
        warm_timeout_s: float = 3600.0,
        health_timeout_s: float = 5.0,
    ) -> None:
        self._query_timeout = query_timeout_s
        self._warm_timeout = warm_timeout_s
        self._health_timeout = health_timeout_s
        self._client = httpx.Client(base_url=base_url, timeout=query_timeout_s)

    def close(self) -> None:
        self._client.close()

    def _post(self, path: str, payload: dict, *, timeout: float) -> dict:
        try:
            response = self._client.post(path, json=payload, timeout=timeout)
        except httpx.HTTPError as exc:
            raise LlamaError(f"llama-server unreachable ({path}): {exc}") from exc
        if response.status_code >= 400:
            raise LlamaError(
                f"llama-server {path} returned {response.status_code}: {response.text[:500]}"
            )
        # A 200 with a non-JSON body (crashed server mid-write, a proxy splash
        # page, a misconfigured LLAMA_SERVER_URL) must surface as LlamaError
        # (-> 502 + the recovery paths), never as a raw JSONDecodeError -> 500.
        try:
            return response.json()
        except ValueError as exc:
            raise LlamaError(
                f"llama-server {path} returned non-JSON: {response.text[:200]!r}"
            ) from exc

    def health(self) -> dict:
        try:
            response = self._client.get("/health", timeout=self._health_timeout)
        except httpx.HTTPError as exc:
            raise LlamaError(f"llama-server unreachable: {exc}") from exc
        if response.status_code != 200:
            raise LlamaError(f"llama-server unhealthy: {response.text[:200]}")
        try:
            return response.json()
        except ValueError as exc:
            raise LlamaError("llama-server /health returned non-JSON body") from exc

    def props(self) -> dict:
        """GET /props — server metadata; ``model_path`` identifies the loaded GGUF.

        The engine uses that as the model fingerprint for cache invalidation:
        llama.cpp's sequence-state files carry no identity of the weights that
        produced them, so a same-geometry model switch would otherwise restore
        stale KV state silently.
        """
        try:
            response = self._client.get("/props", timeout=self._health_timeout)
        except httpx.HTTPError as exc:
            raise LlamaError(f"llama-server unreachable (/props): {exc}") from exc
        if response.status_code >= 400:
            raise LlamaError(
                f"llama-server /props returned {response.status_code}: {response.text[:200]}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise LlamaError("llama-server /props returned non-JSON body") from exc

    def count_tokens(self, text: str) -> int:
        data = self._post("/tokenize", {"content": text}, timeout=self._warm_timeout)
        return len(data.get("tokens", []))

    def chat(
        self,
        messages: list[dict],
        *,
        max_tokens: int,
        temperature: float,
        slot_id: int = 0,
        warm: bool = False,
        json_schema: dict | None = None,
    ) -> dict:
        """One chat completion pinned to a slot, with prompt caching on.

        Returns {"content", "timings", "usage"}. timings.prompt_n is the number
        of prompt tokens actually evaluated — near zero on a cache hit, which is
        the whole point of this project.

        When ``json_schema`` is given, the completion is constrained to emit
        JSON matching it, via the OpenAI wrapper shape llama-server's
        compat layer parses: ``response_format: {"type": "json_schema",
        "json_schema": {"schema": {...}}}``. (The parser reads the schema from
        the nested ``json_schema`` object — a top-level ``schema`` key next to
        ``type: json_schema`` is silently ignored and would leave sampling
        unconstrained.) It drives grammar-based sampling only and never alters
        the messages, so the cached prefix is untouched.
        """
        payload = {
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            # llama.cpp-specific extensions, accepted alongside OpenAI params:
            "id_slot": slot_id,
            "cache_prompt": True,
            "timings_per_token": False,
        }
        if json_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"schema": json_schema},
            }
        timeout = self._warm_timeout if warm else self._query_timeout
        data = self._post("/v1/chat/completions", payload, timeout=timeout)
        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise LlamaError(f"Unexpected llama-server response shape: {data}") from exc
        return {
            "content": content,
            "timings": data.get("timings", {}),
            "usage": data.get("usage", {}),
            # "stop" = finished naturally; "length" = clipped at max_tokens or
            # the context edge. Surfaced so truncation is never silent.
            "finish_reason": choice.get("finish_reason"),
        }

    # --- Slot persistence (requires llama-server --slot-save-path) ---------

    def slot_save(self, filename: str, slot_id: int = 0) -> dict:
        return self._post(
            f"/slots/{slot_id}?action=save", {"filename": filename}, timeout=self._warm_timeout
        )

    def slot_restore(self, filename: str, slot_id: int = 0) -> dict:
        return self._post(
            f"/slots/{slot_id}?action=restore", {"filename": filename}, timeout=self._warm_timeout
        )

    def slot_erase(self, slot_id: int = 0) -> dict:
        return self._post(f"/slots/{slot_id}?action=erase", {}, timeout=self._query_timeout)
