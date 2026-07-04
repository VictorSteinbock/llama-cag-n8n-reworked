"""cag-gate: a fail-safe grounding gate for agent feedback loops.

Verify a candidate fact or action against an immutable, source-grounded canon
(served by cag-api) *before* it enters an agent's memory or drives an action —
so hallucinations cannot compound into persistent drift.

See ``docs/AGENTS.md`` in the llama-cag-n8n repo for the design and per-framework
recipes (Hermes Agent, OpenClaw).
"""

from .client import http_ask, http_verify
from .gate import Action, Decision, GroundingGate, Policy, Verdict

__all__ = [
    "Action",
    "Decision",
    "GroundingGate",
    "Policy",
    "Verdict",
    "http_ask",
    "http_verify",
]

__version__ = "0.1.0"
