# OpenClaw — `cag-verify` skill

[OpenClaw](https://docs.openclaw.ai) has approval gates for high-*impact* actions
but ships **no fact-checking** — so a 24/7 agent can quietly write a wrong "fact"
into `MEMORY.md` and repeat it forever. This skill adds the missing check: a
fail-safe verdict against a **pinned source-of-truth document** in cag-api. The
design and the *why* are in [`../../docs/AGENTS.md`](../../docs/AGENTS.md).

## Contents

- [`cag-verify/SKILL.md`](cag-verify/SKILL.md) — the skill manifest + instructions.
- [`cag-verify/cag_verify.py`](cag-verify/cag_verify.py) — a self-contained,
  stdlib-only checker (no installs beyond `python3`).

## Install

1. Run the stack and ingest your canon (see the repo README).
2. Copy the `cag-verify/` directory into your OpenClaw skills folder (or publish it
   to ClawHub with the `clawhub` CLI).
3. Set the environment variable the skill declares:
   ```bash
   export CAG_API_URL="http://localhost:8000"   # where cag-api is reachable
   ```
4. Tell the agent (in its `SOUL.md` / instructions) to run `cag-verify` before it
   remembers a fact or asserts something to a user.

## Recipes beyond a single check

- **Compliance anchor** — pin your SOP/policy doc; make `cag-verify` a required
  pre-send step for any outbound message, fail-closed to human approval (which
  OpenClaw already supports for high-impact actions).
- **Memory hygiene cron** — a scheduled job that re-runs `cag-verify` over the
  facts in `MEMORY.md` and moves any that come back `block`/`escalate` into a
  quarantine section. Counters the "uncontrolled memory growth" that drives drift.

For programmatic use (your own tools, not a skill), the tested
[`cag_gate`](../cag_gate) Python package exposes the same fail-safe policy as a
`GroundingGate` object.
