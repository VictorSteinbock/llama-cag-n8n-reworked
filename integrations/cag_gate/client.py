"""Zero-dependency HTTP client for the cag-api oracle (stdlib ``urllib`` only).

Kept dependency-free on purpose: these functions are dropped into agent runtimes
(Hermes Agent plugins, OpenClaw skills) that should not have to install anything
beyond the standard library. ``base_url`` defaults to ``$CAG_API_URL`` so the
same code works whether the stack is on localhost or another host.
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Optional


def _base_url(base_url: Optional[str]) -> str:
    return (base_url or os.environ.get("CAG_API_URL", "http://localhost:8000")).rstrip("/")


def _post(path: str, payload: dict, *, base_url: Optional[str], timeout: float) -> dict:
    req = urllib.request.Request(
        _base_url(base_url) + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted local URL)
        return json.loads(resp.read().decode("utf-8"))


def http_verify(
    claim: str,
    *,
    document_id: Optional[int] = None,
    base_url: Optional[str] = None,
    timeout: float = 60.0,
) -> dict:
    """``POST /verify`` — a grounded, reproducible verdict for one claim.

    Returns the raw response dict (``{claim, verdict, quote, quote_grounded, ...}``).
    Raises on transport/HTTP error; :class:`cag_gate.GroundingGate` catches that and
    fails closed.
    """
    payload: dict = {"claim": claim}
    if document_id is not None:
        payload["document_id"] = document_id
    return _post("/verify", payload, base_url=base_url, timeout=timeout)


def http_ask(
    question: str,
    *,
    document_id: Optional[int] = None,
    base_url: Optional[str] = None,
    timeout: float = 120.0,
) -> dict:
    """``POST /query`` — ask the pinned canon a question (Read-grounding)."""
    payload: dict = {"question": question}
    if document_id is not None:
        payload["document_id"] = document_id
    return _post("/query", payload, base_url=base_url, timeout=timeout)
