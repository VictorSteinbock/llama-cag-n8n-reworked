"""Framework-agnostic grounding gate over the cag-api ``POST /verify`` oracle.

The gate turns a verdict from the oracle into a **fail-safe** decision about
whether a candidate fact or action may be trusted. It is deliberately
independent of any agent framework *and* of the transport: construct it with a
``verify`` callable (``claim -> verdict dict``) and it applies the policy. The
stdlib HTTP client in :mod:`cag_gate.client` provides the real ``verify``; the
tests pass a scripted fake.

Why this exists: self-improving agents (Hermes Agent, OpenClaw, ...) evolve a
persistent memory with no ground-truth check, so a hallucinated fact gets
consolidated and then retrieved-and-reinforced — cumulative, persistent drift.
This gate is the **Write-Validation** step: a fact is checked against an
immutable, source-grounded canon *before* it is allowed into memory or acted on.
The canon lives in the cag-api KV cache, outside the agent's memory, so it
cannot be poisoned by the agent's own drift.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional


class Action(str, Enum):
    """What the caller should do with a candidate fact/action."""

    ALLOW = "allow"           # trust it: supported by the canon with a grounded quote
    QUARANTINE = "quarantine"  # keep but tag unverified; never treat as a verified fact
    BLOCK = "block"           # do not persist / do not act: contradicted or fabricated
    ESCALATE = "escalate"     # route to a human: cannot confirm, or the oracle is down


@dataclass
class Verdict:
    """A normalized view of a ``POST /verify`` response (or its absence)."""

    claim: str
    verdict: Optional[str] = None       # "supported" | "absent" | "contradicted" | None
    quote: str = ""
    quote_grounded: Optional[bool] = None
    conditions: str = ""
    match_ratio: Optional[float] = None
    grounding_method: str = ""
    error: Optional[str] = None         # set when the oracle could not be reached / failed

    @classmethod
    def from_response(cls, claim: str, data: object) -> "Verdict":
        if not isinstance(data, dict):
            return cls.unavailable(claim, f"unexpected response type: {type(data).__name__}")
        return cls(
            claim=claim,
            verdict=data.get("verdict"),
            quote=data.get("quote") or "",
            quote_grounded=data.get("quote_grounded"),
            conditions=data.get("conditions") or "",
            match_ratio=data.get("match_ratio"),
            grounding_method=data.get("grounding_method") or "",
        )

    @classmethod
    def unavailable(cls, claim: str, error: str) -> "Verdict":
        return cls(claim=claim, error=error)


@dataclass
class Decision:
    """The gate's ruling on one claim."""

    action: Action
    reason: str
    verdict: Verdict
    tag: Optional[str] = None           # e.g. "unverified", "contradicts-canon"

    @property
    def trusted(self) -> bool:
        """True only when the claim may be treated as a verified fact / safe action."""
        return self.action is Action.ALLOW


@dataclass
class Policy:
    """Fail-safe mapping from verdict to action. Defaults fail *closed*.

    ``absent`` is the honest weak spot: the oracle cannot ground a claim the canon
    does not mention (it may be a hallucination, or legitimately new information).
    So an ``absent`` memory write is quarantined (kept, tagged unverified) rather
    than trusted, and an ``absent`` action is escalated to a human — never
    auto-trusted. Set ``drop_absent`` to turn ``absent`` memory writes into BLOCK.
    """

    on_absent_memory: Action = Action.QUARANTINE
    on_absent_action: Action = Action.ESCALATE
    # A "supported" verdict whose cited quote is NOT in the source (quote_grounded
    # is False) is a fabricated citation — caught mechanically, never trusted.
    require_grounded_quote: bool = True

    @classmethod
    def strict(cls) -> "Policy":
        """Drop unverifiable facts instead of quarantining them."""
        return cls(on_absent_memory=Action.BLOCK)


@dataclass
class GroundingGate:
    """Apply the fail-safe :class:`Policy` to a claim, via a ``verify`` callable."""

    verify: Callable[..., dict]         # claim -> verdict dict (client.http_verify or a fake)
    document_id: Optional[int] = None   # which canon to check against; None = most-recent
    policy: Policy = field(default_factory=Policy)

    def _verdict(self, claim: str) -> Verdict:
        try:
            data = self.verify(claim, document_id=self.document_id)
        except Exception as exc:  # transport / HTTP / decode error -> treat as unavailable
            return Verdict.unavailable(claim, f"{type(exc).__name__}: {exc}")
        return Verdict.from_response(claim, data)

    def evaluate(self, claim: str, *, context: str = "memory") -> Decision:
        """Rule on ``claim``. ``context`` is "memory" (a write) or "action" (a step)."""
        if not claim or not claim.strip():
            return Decision(Action.BLOCK, "empty claim", Verdict(claim=claim), tag="empty")

        v = self._verdict(claim)
        is_action = context == "action"

        if v.error is not None:
            # Fail closed: never silently trust when the oracle is unreachable.
            act = Action.ESCALATE if is_action else Action.QUARANTINE
            return Decision(act, f"oracle unavailable: {v.error}", v, tag="unverified")

        if v.verdict == "supported":
            if self.policy.require_grounded_quote and v.quote_grounded is False:
                act = Action.BLOCK if is_action else Action.QUARANTINE
                return Decision(
                    act,
                    "verdict is 'supported' but the cited quote is not in the source "
                    "(fabricated citation)",
                    v,
                    tag="fabricated-quote",
                )
            reason = "supported by the canon"
            if v.quote:
                reason += f': "{v.quote}"'
            return Decision(Action.ALLOW, reason, v)

        if v.verdict == "contradicted":
            act = Action.BLOCK if is_action else Action.QUARANTINE
            reason = "contradicted by the canon"
            if v.quote:
                reason += f': "{v.quote}"'
            return Decision(act, reason, v, tag="contradicts-canon")

        if v.verdict == "absent":
            act = self.policy.on_absent_action if is_action else self.policy.on_absent_memory
            return Decision(act, "not found in the canon (cannot be grounded)", v, tag="unverified")

        # Unknown / missing verdict -> fail closed.
        act = Action.ESCALATE if is_action else Action.QUARANTINE
        return Decision(act, f"unrecognized verdict: {v.verdict!r}", v, tag="unverified")

    def gate_memory_write(self, fact: str) -> Decision:
        """Should ``fact`` be persisted to the agent's memory as a verified fact?"""
        return self.evaluate(fact, context="memory")

    def gate_action(self, claim: str) -> Decision:
        """Is the factual ``claim`` an action rests on safe to act on?"""
        return self.evaluate(claim, context="action")
