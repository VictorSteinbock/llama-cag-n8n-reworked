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

Hooks
  * ``pre_tool_call``  — **hard gate** on direct writes through the built-in
    memory tool: current Hermes honors ``{"action": "block", "message": ...}``
    from this hook and short-circuits the tool call, so an ungrounded write
    never lands. Older builds ignore hook return values — there this degrades
    to a no-op and the tripwire below still records the conflict. Skipped when
    ``CAG_OVERRIDE_MEMORY=1`` (the memory tool *is* the gate there, and blocking
    would defeat its tagged-episodic storage).
  * ``post_tool_call`` — reactive safety net for those older builds (and for
    memory tools this plugin doesn't know by name): if a direct write did land,
    re-verify it and record the conflict in the quarantine file.
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


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


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
        "recall_overlap": verdict.recall_overlap,
        "tag": decision.tag,
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
    if (
        decision.verdict.verdict == "absent"
        and decision.tag == "unverified"  # NOT "absent-but-topic-present"
        and _truthy("CAG_ABSENT_TO_MEMORY")
    ):
        # Episodic/subjective memories ("user prefers terse answers") are
        # inherently absent from a technical canon. With this flag they stay
        # USABLE in the memory file — visibly tagged, never verified — instead
        # of flooding the quarantine file. Recommended with the hard gate
        # (CAG_OVERRIDE_MEMORY=1), where ALL writes pass through here.
        # The tag guard matters: when the oracle's recall probe shows the canon
        # DOES discuss the claim's vocabulary, "absent" may be a missed passage
        # or a twisted claim — that case escalates and is never stored, not
        # even tagged.
        _append(_memory_path(), f"- [unverified] {fact}")
        return json.dumps({
            "stored": True, "verified": False, "tagged": "unverified",
            "reason": decision.reason,
        })
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


def _on_pre_tool_call(tool_name, args, task_id=None, **kwargs):
    """Hard gate on direct built-in memory writes. Current Hermes honors a
    ``{"action": "block", "message": ...}`` return from ``pre_tool_call`` (the
    tool call is short-circuited and ``message`` is returned to the model as the
    error); older builds ignore the return value, where this is a no-op and the
    ``post_tool_call`` tripwire still records the conflict after the fact."""
    if _truthy("CAG_OVERRIDE_MEMORY"):
        return None  # the memory tool IS cag_remember already — it self-gates
    if tool_name not in _BUILTIN_MEMORY_TOOLS:
        return None
    fact = (args or {}).get("fact") or (args or {}).get("content") or ""
    if not fact.strip():
        return None
    decision = _gate().gate_memory_write(fact)
    if decision.trusted:
        return None
    _append(_quarantine_path(), f"- [pre-block:{decision.tag}] {fact}  ({decision.reason})")
    return {
        "action": "block",
        "message": (
            f"memory write blocked by the CAG grounding gate: {decision.reason}. "
            "Store facts through cag_remember (verified writes) or re-check the source."
        ),
    }


def _on_post_tool_call(tool_name, args, result, task_id=None, **kwargs) -> None:
    """Reactive net for builds whose hooks cannot veto (and for memory tools not
    in ``_BUILTIN_MEMORY_TOOLS``): a landed write can't be undone here, but the
    conflict is recorded so a hygiene pass / human can act — and future turns
    see the flag."""
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
    # Two ways to hard-gate the built-in memory tool, pick per taste:
    #   1. (default) the pre_tool_call hook below BLOCKS ungrounded writes on
    #      current Hermes — the model gets the gate's reason as a tool error.
    #   2. CAG_OVERRIDE_MEMORY=1 replaces the built-in memory tool with
    #      cag_remember outright — writes come back as stored/quarantined/tagged
    #      results instead of errors (and CAG_ABSENT_TO_MEMORY tagging works).
    #      The pre-hook steps aside when the override is on.
    # On older Hermes builds whose hooks cannot veto, option 2 is the only hard
    # gate and the hooks are a reactive tripwire. (Match the schema to your
    # Hermes version's memory tool if it differs.)
    if _truthy("CAG_OVERRIDE_MEMORY"):
        ctx.register_tool(
            name="memory", toolset=TOOLSET, schema=_FACT_SCHEMA,
            handler=cag_remember_tool, override=True,
        )
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
