---
name: cag-verify
description: Fact-check a claim against a pinned source-of-truth document using a local cag-api grounding oracle, returning a fail-safe verdict before you store it to memory or send it to a user.
version: 1.0.0
metadata:
  openclaw:
    requires:
      env:
        - CAG_API_URL
      bins:
        - python3
    primaryEnv: CAG_API_URL
---

# cag-verify

Check a factual claim against a **pinned source-of-truth document** (a policy,
spec, contract, or handbook that has been ingested into cag-api) and get a
fail-safe verdict. Use it to keep hallucinations out of your memory and out of
what you tell people.

## When to use it

Run this **before** you:

- write a factual claim to memory (`MEMORY.md`), or
- send a factual statement to a user, or
- take an action whose justification is a claimed fact.

## How to run it

Run the bundled checker (it sits next to this `SKILL.md`) with the claim as one
argument:

```bash
python3 cag_verify.py "The refund window is 30 days"
```

It prints one JSON line and sets an exit code.

- **exit 0** → `{"trusted": true, "action": "allow", ...}` — the canon supports the
  claim with a real, grounded quote. Safe to store or send. Cite the `quote`.
- **exit 1** → `{"trusted": false, ...}` — do **not** store it as a fact or state it
  as true. Read `action`:
  - `block` → the canon **contradicts** the claim, or the model's cited quote was
    fabricated (not actually in the source). Drop the claim.
  - `escalate` → the canon does **not mention** it, or the oracle was unreachable.
    Don't guess — ask the user or a human.

## The rule

`trusted: false` means **unverified**. Never promote an unverified claim to a
remembered fact or a confident statement. When in doubt, quote the `reason` back
to the user and ask.
