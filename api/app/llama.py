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
        return response.json()

    def health(self) -> dict:
        try:
            response = self._client.get("/health", timeout=self._health_timeout)
        except httpx.HTTPError as exc:
            raise LlamaError(f"llama-server unreachable: {exc}") from exc
        if response.status_code != 200:
            raise LlamaError(f"llama-server unhealthy: {response.text[:200]}")
        return response.json()

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
    ) -> dict:
        """One chat completion pinned to a slot, with prompt caching on.

        Returns {"content", "timings", "usage"}. timings.prompt_n is the number
        of prompt tokens actually evaluated — near zero on a cache hit, which is
        the whole point of this project.
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
        timeout = self._warm_timeout if warm else self._query_timeout
        data = self._post("/v1/chat/completions", payload, timeout=timeout)
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise LlamaError(f"Unexpected llama-server response shape: {data}") from exc
        return {
            "content": content,
            "timings": data.get("timings", {}),
            "usage": data.get("usage", {}),
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
