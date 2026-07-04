# integrations/ — the grounding gate for agent loops

Drop the llama-cag **grounding oracle** into an agent's feedback loop so a
hallucinated fact can't be written to memory and then retrieved-and-reinforced
into persistent drift. The full design, the drift mechanism, and the honest
limits are in **[`../docs/AGENTS.md`](../docs/AGENTS.md)**; this directory is the
code.

## What's here

| Path | What it is |
|---|---|
| [`cag_gate/`](cag_gate) | The tested, framework-agnostic core: a `GroundingGate` that turns a `POST /verify` verdict into a fail-safe `Decision` (allow / quarantine / block / escalate). Stdlib only. |
| [`hermes/`](hermes) | A [Hermes Agent](https://hermes-agent.nousresearch.com) plugin: `cag_verify` / `cag_ask` / `cag_remember` tools + a reactive memory-quarantine hook. |
| [`openclaw/`](openclaw) | A [OpenClaw](https://docs.openclaw.ai) `cag-verify` skill (`SKILL.md` + a self-contained checker) — the fact-check OpenClaw doesn't ship. |
| [`tests/`](tests) | The fail-safe matrix + the before/after "compounding loop is broken" trace. |

## Quick start

```bash
# 1. run the stack and ingest your canon (see the repo README), then:
pip install -e ./integrations           # exposes the `cag_gate` package
pytest integrations                      # 11 tests, no services needed
```

Programmatic use of the core:

```python
from cag_gate import GroundingGate, http_verify

gate = GroundingGate(verify=http_verify, document_id=7)   # 7 = your pinned canon
d = gate.gate_memory_write("The API rate limit is 1000 req/s")
if not d.trusted:
    print(d.action.value, "-", d.reason)   # e.g. block - contradicted by the canon: "...100/s..."
```

Then wire it to your framework with the [Hermes](hermes) or [OpenClaw](openclaw)
adapter, or call `POST /verify` directly from any other runtime.

## The fail-safe policy in one table

| `/verify` result | Memory write | Action gate |
|---|---|---|
| `supported` + quote in source | **allow** | **allow** |
| `supported` but quote fabricated | quarantine | block |
| `supported` but quote too short/generic to be evidence | quarantine | escalate |
| `contradicted` | quarantine | block |
| `absent` (not in canon) | quarantine (tag unverified) | escalate to human |
| oracle unreachable / unknown | quarantine | escalate |

Only `supported` with a **grounded, non-trivial** quote is ever trusted — the
byte-check proves a quote's *existence*, not its sufficiency, so quotes under
the evidence floor (`Policy.min_grounded_quote_chars`, default 12 collapsed
chars; a generic "is the" grounds in any document) are never counted as
evidence. Everything else is kept out of "verified" memory or routed to a
human — fail closed.
