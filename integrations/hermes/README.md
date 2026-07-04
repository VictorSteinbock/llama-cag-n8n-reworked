# Hermes Agent — grounding gate plugin

Give a [Hermes Agent](https://hermes-agent.nousresearch.com) a **pinned canon**
(a spec, policy, contract, runbook) and gate its memory and answers against it,
so a hallucinated fact can't be consolidated into episodic memory and then
retrieved-and-reinforced on a later turn. The design and the *why* are in
[`../../docs/AGENTS.md`](../../docs/AGENTS.md); this is the install.

## What it adds

| Surface | Name | Role |
|---|---|---|
| tool | `cag_verify(claim)` | fail-safe verdict for a factual claim (Read-grounding) |
| tool | `cag_ask(question)` | ask the canon a question (Read-grounding) |
| tool | `cag_remember(fact)` | **Write-Validation** — verify *before* persisting; grounded facts are saved, the rest go to a quarantine file |
| hook | `post_tool_call` | reactive net: flags a *direct* built-in memory write that the canon contradicts |
| hook | `pre_llm_call` | injects a one-line grounding reminder each turn (optional) |

The decision logic lives in the unit-tested [`cag_gate`](../cag_gate) package;
`cag_plugin.py` is only the Hermes wiring.

## Install

1. **Run the stack** and ingest your canon (see the repo README). Note its
   document id from `GET /documents` if you want to pin one.
2. **Install the gate** into the same Python environment Hermes runs in:
   ```bash
   pip install -e /path/to/llama-cag-n8n/integrations
   ```
3. **Register the plugin** with Hermes (copy or symlink `cag_plugin.py` into your
   Hermes plugins directory, per
   [Build a Hermes Plugin](https://hermes-agent.nousresearch.com/docs/guides/build-a-hermes-plugin)).
4. **Point it at the API** (environment):

   | Variable | Default | Meaning |
   |---|---|---|
   | `CAG_API_URL` | `http://localhost:8000` | Where cag-api is reachable |
   | `CAG_DOCUMENT_ID` | *(unset = most-recent)* | Which canon to check against |
   | `CAG_MEMORY_PATH` | `MEMORY.md` | Where verified facts are appended |
   | `CAG_MEMORY_QUARANTINE_PATH` | `MEMORY.quarantine.md` | Where rejected facts are diverted |

5. **Tell the agent to use it.** Add one line to your Hermes system prompt:
   > To remember a fact, call `cag_remember` (it verifies against the canon). Before
   > acting on a factual claim, call `cag_verify`. Treat any non-`supported` result
   > as unverified.

## Enforcement strength (be honest about it)

Hermes `pre_tool_call`/`post_tool_call` hooks are **observers** — their return
value is ignored, so a hook cannot *veto* a write. That leaves two levels:

- **Soft (default):** the agent is instructed to use `cag_remember`, and
  `post_tool_call` flags any direct built-in memory write the canon contradicts.
  Good hygiene; relies on the agent following instructions.
- **Hard:** in `register()`, register `cag_remember` with `override=True` under the
  built-in memory tool's name so *every* memory write is gated regardless of what
  the model chooses. Match your Hermes version's memory-tool arg schema when you do
  (there's a commented example in `cag_plugin.py`).

## What "gated" looks like

A `cag_remember("The API rate limit is 1000 req/s")` when the pinned spec says 100:

```json
{"stored": false, "quarantined": true, "action": "quarantine",
 "reason": "contradicted by the canon: \"requests are capped at 100/s (Sec 4.2)\"",
 "note": "NOT stored as a verified fact. ..."}
```

The false fact never enters `MEMORY.md`, so it can't resurface on a later turn.
