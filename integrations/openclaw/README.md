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

## Hard-gating memory writes with a plugin hook

A skill asks the model to check; a hook makes the runtime enforce. Current
OpenClaw plugins can veto tool calls from `before_tool_call` — return
`{ block: true, blockReason }` to refuse the call outright, or
`{ requireApproval: true }` to pause it for the human approval flow OpenClaw
already has. A minimal gate wired to this API:

```js
// cag-gate hook sketch — written against docs.openclaw.ai/plugins/hooks
// (mid-2026). Hook names/shapes move; verify against your installed version.
api.on("before_tool_call", async ({ toolName, params }) => {
  if (!/^memory/.test(toolName)) return;              // gate only memory writes
  const fact = params?.content ?? params?.fact ?? "";
  if (!fact.trim()) return;
  let v;
  try {
    const res = await fetch(`${process.env.CAG_API_URL}/verify`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ claim: fact }),
    });
    v = await res.json();
  } catch {
    return { block: true, blockReason: "grounding oracle unreachable — failing closed" };
  }
  const evidence = (v.quote || "").replace(/[\u200B\u200C\u200D\uFEFF]/g, "").trim();
  const grounded = v.verdict === "supported" && v.quote_grounded === true
    && evidence.length >= 12;                          // the evidence floor
  if (grounded) return;                                // let the write through
  if (v.verdict === "absent" && (v.recall?.max_overlap ?? 0) >= 0.5)
    return { requireApproval: true };                  // topic present: human decides
  return { block: true, blockReason: `CAG gate: ${v.verdict} — not stored (${v.quote || "no quote"})` };
});
```

Same fail-closed table as the Python gate ([docs/AGENTS.md](../../docs/AGENTS.md#the-fail-safe-policy));
the tested reference logic lives in [`cag_gate`](../cag_gate) if you'd rather
port it wholesale. Prefer this hook over re-registering the memory tool from a
plugin — dynamic tool re-registration has open reliability issues upstream.

For programmatic use (your own tools, not a skill), the tested
[`cag_gate`](../cag_gate) Python package exposes the same fail-safe policy as a
`GroundingGate` object.
