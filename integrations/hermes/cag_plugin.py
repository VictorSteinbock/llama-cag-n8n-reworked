"""Hermes Agent plugin — ground the agent's memory and answers against a canon.

This wires the tested :mod:`cag_gate` grounding gate into the documented Hermes
Agent plugin API. It registers three tools and two hooks:

Tools
  * ``cag_verify(claim)``   — fail-safe verdict for a factual claim (Read-grounding).
  * ``cag_ask(question)``   — ask the pinned canon a question (Read-grounding).
  * ``cag_remember(fact)``  — **Write-Validation**: verify a fact *before* it is
    persisted. Grounded facts are appended to the memory file; contradicted,
    absent, or fabricated-quote facts are diverted to a quarantine file and NOT
    stored as verified. Instruct the agent (system prompt) to use this instead of
    the built-in ``memory`` tool — or set ``CAG_OVERRIDE_MEMORY=1`` to replace
    the built-in ``memory`` tool outright (hard enforcement; see README).

Hooks (observers — Hermes ignores their return value except ``pre_llm_call``)
  * ``post_tool_call`` — reactive safety net: if the agent used the *built-in*
    memory tool directly, re-verify the written text and quarantine it if the
    canon contradicts it.
  * ``pre_llm_call``   — inject a one-line grounding reminder into each turn
    (optional; delete the registration if you don't want it).

Only ``register(ctx)`` touches Hermes; all the decision logic lives in
:mod:`cag_gate` and is unit-tested. Signatures follow the documented plugin API
(register / register_tool / register_hook); if your installed Hermes version
differs, adjust *this* file only — the gate does not change. Requires
``pip install -e /path/to/llama-cag-n8n/integrations`` and a reachable cag-api
(``CAG_API_URL``, default ``http://localhost:8000``).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from cag_gate import GroundingGate, http_ask, http_verify

TOOLSET = "cag"


# --- configuration (all via environment) -------------------------------------

def _document_id():
    raw = os.environ.get("CAG_DOCUMENT_ID")
    return int(raw) if raw and raw.strip() else None


def _gate() -> GroundingGate:
    return GroundingGate(verify=http_verify, document_id=_document_id())


def _memory_path() -> Path:
    return Path(os.environ.get("CAG_MEMORY_PATH", "MEMORY.md"))


def _quarantine_path() -> Path:
    return Path(os.environ.get("CAG_MEMORY_QUARANTINE_PATH", "MEMORY.quarantine.md"))


def _append(path: Path, line: str) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line.rstrip("\n") + "\n")
        return True
    except OSError:
        return False


# --- tool handlers (return a JSON string, always — even on error) ------------

def cag_verify_tool(args: dict, **kwargs) -> str:
    claim = (args or {}).get("claim", "")
    decision = _gate().gate_action(claim)
    verdict = decision.verdict
    return json.dumps({
        "trusted": decision.trusted,
        "action": decision.action.value,
        "reason": decision.reason,
        "verdict": verdict.verdict,
        "quote": verdict.quote,
        "quote_grounded": verdict.quote_grounded,
        "conditions": verdict.conditions,
    })


def cag_ask_tool(args: dict, **kwargs) -> str:
    question = (args or {}).get("question", "")
    try:
        data = http_ask(question, document_id=_document_id())
    except Exception as exc:  # handlers must never raise; return JSON error instead
        return json.dumps({"error": f"{type(exc).__name__}: {exc}"})
    return json.dumps({"answer": data.get("answer", ""), "document": data.get("document")})


def cag_remember_tool(args: dict, **kwargs) -> str:
    fact = (args or {}).get("fact") or (args or {}).get("content") or ""
    decision = _gate().gate_memory_write(fact)
    if decision.trusted:
        _append(_memory_path(), f"- {fact}")
        return json.dumps({"stored": True, "verified": True, "reason": decision.reason})
    _append(_quarantine_path(), f"- [{decision.tag}] {fact}  ({decision.reason})")
    return json.dumps({
        "stored": False,
        "quarantined": True,
        "action": decision.action.value,
        "reason": decision.reason,
        "note": (
            "NOT stored as a verified fact. The canon did not support it. "
            "Do not rely on this claim; re-check the source or ask the user."
        ),
    })


# --- hooks -------------------------------------------------------------------

_BUILTIN_MEMORY_TOOLS = {"memory", "remember", "save_memory"}


def _on_post_tool_call(tool_name, args, result, task_id=None, **kwargs) -> None:
    """Reactive net: if the agent wrote memory *directly*, quarantine it if the
    canon contradicts it. Cannot un-write (hooks are observers), but records the
    conflict so a hygiene pass / human can act — and future turns see the flag."""
    if tool_name not in _BUILTIN_MEMORY_TOOLS:
        return
    fact = (args or {}).get("fact") or (args or {}).get("content") or ""
    if not fact.strip():
        return
    decision = _gate().gate_memory_write(fact)
    if not decision.trusted:
        _append(_quarantine_path(), f"- [direct-write:{decision.tag}] {fact}  ({decision.reason})")


def _on_pre_llm_call(**kwargs):
    """Inject a short grounding reminder each turn. Optional — remove the
    registration below to disable. Returning a dict with a ``context`` key is the
    documented way for a plugin to add text to the current turn."""
    return {
        "context": (
            "A source-of-truth canon is pinned via the cag tools. Before you store a "
            "fact use cag_remember (it verifies against the canon); before you act on a "
            "factual claim use cag_verify. Treat any 'quarantined' or non-'supported' "
            "result as unverified."
        )
    }


# --- registration (the only Hermes-specific surface) -------------------------

_CLAIM_SCHEMA = {
    "type": "object",
    "properties": {"claim": {"type": "string", "description": "The factual claim to check."}},
    "required": ["claim"],
}
_QUESTION_SCHEMA = {
    "type": "object",
    "properties": {"question": {"type": "string", "description": "A question for the canon."}},
    "required": ["question"],
}
_FACT_SCHEMA = {
    "type": "object",
    "properties": {"fact": {"type": "string", "description": "A fact to remember, if grounded."}},
    "required": ["fact"],
}


def register(ctx):
    """Called once at startup by Hermes Agent."""
    ctx.register_tool(
        name="cag_verify", toolset=TOOLSET, schema=_CLAIM_SCHEMA, handler=cag_verify_tool,
    )
    ctx.register_tool(
        name="cag_ask", toolset=TOOLSET, schema=_QUESTION_SCHEMA, handler=cag_ask_tool,
    )
    ctx.register_tool(
        name="cag_remember", toolset=TOOLSET, schema=_FACT_SCHEMA, handler=cag_remember_tool,
    )
    # Hard enforcement: CAG_OVERRIDE_MEMORY=1 replaces the built-in memory tool,
    # so EVERY memory write goes through the gate. Without it the cag_* tools are
    # companions the agent must be instructed to prefer, and the post_tool_call
    # hook below is only a reactive tripwire — Hermes hooks are observers and
    # cannot veto a write that already happened. (Match the schema to your
    # Hermes version's memory tool if it differs.)
    if os.environ.get("CAG_OVERRIDE_MEMORY", "").strip().lower() in {"1", "true", "yes", "on"}:
        ctx.register_tool(
            name="memory", toolset=TOOLSET, schema=_FACT_SCHEMA,
            handler=cag_remember_tool, override=True,
        )
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
