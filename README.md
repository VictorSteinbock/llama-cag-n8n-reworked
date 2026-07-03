<p align="center">
  <img src="docs/images/hero.svg" alt="llama-cag-n8n — Read once. Ask forever." width="100%">
</p>

# llama-cag-n8n

**Ask questions about your documents with a fully local LLM — without the model
re-reading the document every time.**

[![CI](https://github.com/VictorSteinbock/llama-cag-n8n-reworked/actions/workflows/ci.yml/badge.svg)](https://github.com/VictorSteinbock/llama-cag-n8n-reworked/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-34D399.svg)](LICENSE)

> **Not technical?** Read **[the two-minute, plain-words version](docs/EXPLAINER.md)**
> — a brilliant reader, a filing clerk, and why reading once beats re-reading forever.

This is a self-hosted implementation of **Cache-Augmented Generation (CAG)**: a
document is processed by the model **once**, the resulting KV cache (the model's
internal state after reading it) is **saved to disk**, and every later question
**restores** that state — so only your question and the answer are ever computed
again. On CPU hardware, that turns minutes of prompt processing into seconds.

Everything runs in Docker: [llama.cpp's `llama-server`](https://github.com/ggml-org/llama.cpp)
for inference and cache persistence, a small FastAPI orchestrator (`cag-api`),
[n8n](https://n8n.io/) for automation, and PostgreSQL for metadata. Works on
Windows, macOS, and Linux — no host compilation, no external APIs.

## The economics

<p align="center">
  <img src="docs/images/warm-once.svg" alt="The expensive read happens once per document and survives restarts." width="100%">
</p>

The expensive part — the model reading the whole document — happens **exactly
once per document**, and the resulting KV cache survives restarts. Ingesting a
28k-token manual takes minutes on CPU; every question after that evaluates only
a few dozen tokens. The `/query` response proves it in the `timings` block —
after the first query, `prompt_tokens_evaluated` collapses to your question's
length while `cache_source` reports where the state came from:

```jsonc
// POST /query {"question": "What are the safety limits in section 4?"}
{
  "answer": "Section 4 caps continuous load at 8 A and peak at 12 A for 10 s …",
  "document": { "id": 7, "file_name": "manual.pdf", "n_tokens": 28400 },
  "duration_ms": 640,
  "timings": {
    "prompt_tokens_evaluated": 43,      // just the question — not the 28,400-token doc
    "prompt_tokens_from_cache": 28400,  // the whole document, reused from the KV cache
    "answer_tokens": 96,
    "cache_source": "memory"            // "memory" hot · "disk" restored · "recomputed" self-heal
  }
}
```

The very first query on a document (or the first after `cache_source: "disk"`
following a restart) evaluates the full prefix once; from then on it's tens of
tokens. **Numbers, not adjectives — that's the whole point.** (The JSON above
is an illustrative response — the *shape* is guaranteed by the mechanism, and
your own first query prints the real receipt.)

## Cache states and latency — the behaviour model

Earlier versions made you choose between hand-managed modes (warm-up, basic,
fallback, disabled). v2 has **one automatic policy** and three observable
states — and every answer's `timings.cache_source` tells you which one served it:

| State | When it happens | Added latency | Memory effect |
|-------|-----------------|---------------|---------------|
| `memory` — hot in a slot | The document was ingested or queried recently; up to `CAG_SLOTS` documents stay hot at once (least-recently-used gets evicted) | **None** — only your question and the answer are computed | Uses the slot's share of the fixed KV pool |
| `disk` — restored | First query on a document after a restart or eviction | **Seconds** — the saved KV state is read from disk; the text is *not* re-processed | Loads into a slot (evicting the LRU document if all slots are busy) |
| `recomputed` — self-heal | Cache file missing or invalidated (e.g. you switched models) | The one-time full read, **once** — then it re-saves itself and is fast again | Same as a fresh warm |

Three strategy decisions are baked in, so there is nothing to manage:

- **Warm-at-ingest.** The expensive read happens when a document is *added*
  (ingest returns only after the cache is saved to disk) — so the first
  question is never the slow one. Latency is paid where you expect it: at drop
  time, visibly, once.
- **Always-warm server.** v1's "warm-up mode" (a persistent model instance held
  in RAM) is simply how llama-server always runs now — generalized to
  `CAG_SLOTS` simultaneous hot documents. And v1's 8,000-character fallback
  mode — the silent truncator behind "spotty" answers — is gone entirely:
  there is no degraded path, only the three honest states above.
- **Fresh context by default.** A `/query` without `history` is a clean-room
  question against the document; pass `history` when you *want* a conversation.

**Memory behaviour, precisely:** RAM usage is the model weights plus **one
fixed KV pool** sized by `LLAMA_CTX_SIZE` (halved by `q8_0`), allocated at
startup — it does **not** grow as you add documents. Documents cost *disk*
instead (one cache file each; the nightly maintenance report shows
`cache_bytes`). So a hundred ingested documents and one ingested document use
the same RAM — the slots just decide which few are instant at any moment.

## Where this shines

The mechanism is generic, but it pays off hardest in a specific shape of
problem: **many questions against one dense document that doesn't change.**
Four places that shape shows up.

**A support bot that actually knows the manual.** Narrow-domain help bots built
on a general model hallucinate policy the moment a question leaves the beaten
path — they were never given the source of truth, only a paraphrase of it. Here
the bot's backend is the n8n query webhook, and the model literally holds the
entire product manual or policy document in its KV state, so every answer is
grounded in the actual text. After the one-time warm it's precise and fast, costs
nothing per question, and never sends a word off the machine.

**A token-saver sidecar for cloud coding agents (via MCP).** Give Claude Code a
28k-token spec to work against and it occupies a seventh of a 200k context
window all session — crowding out real work — and while provider-side prompt
caching discounts warm re-sends, those caches are short-lived and still
metered: every fresh session, every post-compaction re-read, pays for the full
spec again. Point the agent at the local `ask_document` MCP tool instead and
only questions (~tens of tokens) and answers cross the boundary. The spec stays
pinned in a local KV cache — never occupying the agent's context, never
expiring, never billed, never leaving your machine.

**The team reference desk.** Contracts, runbooks, compliance manuals, an SOP
binder — documents a team asks the same questions against for weeks, and that
are not allowed to leave the building. This runs entirely on your own hardware,
the caches survive restarts (ask today, ask again after a reboot, no re-warm),
and every answer is grounded in the whole document rather than the top-k chunks a
retriever happened to pull. Private by construction, correct by construction.

**An automation brain inside n8n.** Because querying is a single HTTP call, any
workflow can consult the canonical document as one node: classify an incoming
ticket against the support policy, extract the required fields per the spec,
route an approval by the rulebook. The document is the source of truth and the
workflow just asks it — no embeddings pipeline, no vector store, no glue.

If the shape of your problem is "many questions, one dense document," this is the
cheapest correct setup that runs on your own hardware.

### Loops and living documents

The economics get better the more you loop. **Question sweeps:** because the
marginal question is nearly free, running *hundreds* of questions against one
warm document is a rounding error — a compliance checklist, an extraction
battery, an eval harness. The bundled sweep workflow takes a list and returns
all the answers in one call:

```bash
curl -X POST http://localhost:5678/webhook/cag/sweep \
  -H "Content-Type: application/json" \
  -d '{"questions": ["What is the max load?", "Who signs off?", "Renewal date?"]}'
```

**Living documents:** re-drop a changed file into the watch folder and the hash
dedupe sorts it out — unchanged re-drops are free no-ops, changed versions
re-warm automatically, and the query webhook always answers against the latest
cached state. Point a nightly export at the folder and you have a self-updating
document memory. **Agent loops:** via MCP (or `history` on `/query`), an agent
can interrogate a pinned document iteratively — plan, ask, refine, ask again —
while only the questions and answers ever occupy the agent's own context.

**Scaling up:** all of this multiplies with RAM. A 128 GB unified-memory machine
(or a 256–512 GB Mac Studio) holds a whole shelf of 100k-token documents hot in
parallel slots — see [docs/HARDWARE.md](docs/HARDWARE.md) for per-tier model
recommendations, `.env` presets, and the native-Mac (Metal) recipe.

## Architecture

<p align="center">
  <img src="docs/images/architecture.svg" alt="Four containers in Docker Compose: n8n, cag-api, llama-server, PostgreSQL, with models and kv_caches volumes." width="100%">
</p>

Four containers, one compose file, everything bound to `127.0.0.1`. **n8n**
watches a folder and exposes a query webhook; **cag-api** owns all
orchestration (registry, slot assignment, self-healing); **llama-server** does
inference and persists KV state to the `kv_caches` volume via `--slot-save-path`;
**PostgreSQL** holds document metadata and the query log. n8n carries zero
business logic and zero credentials — it only calls `cag-api` over the internal
Docker network. The full sequence diagrams (warm and query paths) are in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## CAG vs RAG, honestly

<p align="center">
  <img src="docs/images/cag-vs-rag.svg" alt="RAG recomputes retrieved chunks every query; CAG restores the whole-document KV cache and evaluates only your question." width="100%">
</p>

| | RAG | CAG (this project) |
|---|---|---|
| Document handling | chunk → embed → vector DB → retrieve per query | model reads the whole document once, state cached |
| Per-query cost | retrieval + re-processing of retrieved chunks | question + answer tokens only |
| Answer context | top-k chunks | the entire document, always |
| Corpus size | effectively unlimited | **must fit the context window** — that's the trade-off |

If your knowledge base is a handful of manuals, contracts, or specs (up to
~100k tokens each with the default model), CAG is simpler and often more
accurate — the model always sees the *entire* document, not retrieved chunks.
If it's ten thousand documents, you want RAG — and a different repo.

## Why not just Open WebUI or AnythingLLM?

Because they solve a different problem, and this fills a gap none of them do.

**They own retrieval.** AnythingLLM, Open WebUI, and LibreChat are excellent
self-hosted chat UIs with document workspaces — but they are all **RAG systems**
(chunk → embed → vector DB → retrieve per query). They shine on big corpora and
multi-user setups, and they approximate by design: the model sees retrieved
chunks, not the whole document. LM Studio / Ollama / Jan make running a local
model easy and cache prompts in RAM per session, but nothing is document-pinned
and nothing survives a restart.

**This owns exact, persistent whole-document memory.** Feed it a document once;
the model's internal state (KV cache) is persisted to disk. Every question
afterwards — today, tomorrow, after a reboot — skips re-reading and evaluates
only your question, against the *entire* document. Persistent KV-cache reuse is
productized in the cloud (Anthropic/OpenAI/Gemini prompt caching) and active in
research, but **no mainstream self-hosted tool ships it as a feature.** It's also
automation-first: the n8n layer turns folder-drop and a webhook into the primary
interface, not an afterthought.

**Use one of them instead if** you have thousand-document knowledge bases (that's
retrieval territory), documents bigger than the context window, or a team that
needs multi-user auth. This project is **for** the person with a handful of dense
reference documents — manuals, contracts, rulebooks, specs, theses — who asks
repeated questions over weeks on consumer hardware and wants automation hooks
around it. Knowing your lane is the point.

## The family

One engine (`llama-server` + the `cag-api` orchestrator), three faces on it —
plus a desktop control room:

- **The API** (`cag-api`) — the typed HTTP surface. Everything programmatic
  lives here.
- **n8n automation** — folder-drop ingestion, a query webhook, scheduled
  maintenance. The automation-first face.
- **The MCP server** (`cag-mcp`) — the stack as a local document-memory tool for
  Claude Code, Claude Desktop, and other agents (see
  [Use it from Claude Code](#use-it-from-claude-code-mcp)).
- **[LlamaCag UI](https://github.com/VictorSteinbock/LlamaCagUI)** — the desktop
  control room: chat, documents, stack health, model switching. *(Being rebuilt
  against this v2 API — check the repo for status.)*

Ancestry: this is a ground-up rebuild of the original
[AbelCoplet/llama-cag-n8N](https://github.com/AbelCoplet/llama-cag-n8N), which
had the right idea before llama.cpp's slot save/restore made honest CAG a config
option instead of a science project.

## Quick start

**Prerequisites:** [Docker Desktop](https://www.docker.com/products/docker-desktop/)
(or docker + compose v2) and Python 3.10+.

```bash
git clone https://github.com/VictorSteinbock/llama-cag-n8n-reworked.git
cd llama-cag-n8n-reworked

python llamacag.py setup     # writes .env with generated secrets
python llamacag.py start     # builds + starts the stack
```

First boot downloads the default model (Gemma 4 12B QAT, ~6.5 GB) from Hugging
Face into a Docker volume; after that everything runs offline. Watch progress
with `python llamacag.py logs llama-server`, and confirm readiness with
`python llamacag.py status`.

**Set up n8n (one-time, ~2 minutes):**

1. Open http://localhost:5678 and create the local owner account.
2. Import the four workflows from [`n8n/workflows/`](n8n/workflows/)
   (*Workflows → ⋯ → Import from file*).
3. Activate each one. **No credentials to configure** — the workflows only call
   `cag-api` over the internal Docker network.

**Use it:**

```bash
# 1. Make a document queryable (or just drop a file into ./documents/)
curl -X POST http://localhost:8000/documents -F "file=@manual.pdf"

# 2. Ask questions — via n8n's webhook…
curl -X POST http://localhost:5678/webhook/cag/query \
  -H "Content-Type: application/json" \
  -d '{"question": "What are the safety limits in section 4?"}'

# …or directly, or with the helper:
python llamacag.py query "What are the safety limits in section 4?"
```

The response includes `timings.cache_source` (`memory` / `disk` / `recomputed`)
and how many prompt tokens were actually evaluated — after the first query
that number should be tiny.

## The API

Interactive docs at http://localhost:8000/docs.

| Endpoint | What it does |
|----------|--------------|
| `POST /documents` | Multipart file upload (`.txt` `.md` `.html` text-based `.pdf`) → extract, token-count, warm the KV cache, persist it |
| `POST /documents/text` | Same for raw text: `{"text": "...", "file_name": "notes.txt"}` |
| `GET /documents` | Registry with status, token counts, usage |
| `DELETE /documents/{id}` | Remove document + its cache file |
| `POST /query` | `{"question": "...", "document_id"?: n, "max_tokens"?: n, "temperature"?: x, "history"?: [{role, content}, …]}` — no `document_id` means the most recently cached document; `history` enables multi-turn chat (earlier turns stay KV-cached, so each round only evaluates the newest exchange) |
| `POST /maintenance` | Reconcile disk ↔ DB: delete orphaned caches, report missing ones, disk usage |
| `GET /health` | 200 healthy / 503 degraded, with per-dependency detail |

Duplicate content (same SHA-256) is detected and never re-warmed. A document
that doesn't fit the context window is rejected at ingest with a `413` telling
you the measured token count and which knob to raise.

## Use it from Claude Code (MCP)

The `mcp/` package (`cag-mcp`) exposes the stack to any [MCP](https://modelcontextprotocol.io)
client — Claude Code, Claude Desktop, or any 2026 agent — as a local
document-memory tool. Instead of pasting a dense spec into the agent's context on
every task and paying to re-read it each turn, the agent calls the `ask_document`
tool: only the question and the answer cross the boundary, while the document
stays pinned in a local KV cache the cloud model never has to carry. It's a thin
stdio server (`python -m cag_mcp`) that just forwards to `cag-api` at
`CAG_API_URL` (default `http://localhost:8000`), and it offers four tools —
`list_documents`, `ask_document`, `ingest_text`, `ingest_file`.

Register it with Claude Code (`pip install -e ./mcp` first, in the stack repo):

```bash
claude mcp add cag -- python -m cag_mcp
```

…or add it to a project's `.mcp.json`:

```jsonc
{
  "mcpServers": {
    "cag": {
      "command": "python",
      "args": ["-m", "cag_mcp"],
      "env": { "CAG_API_URL": "http://localhost:8000" }
    }
  }
}
```

<p align="center">
  <img src="docs/images/claude-code-mcp.svg" alt="Claude Code calls the local ask_document tool; only questions and answers travel, the spec stays pinned locally." width="100%">
</p>

**What a real coding session looks like.** You ingested the vendor spec once
(dropped it in the watch folder); now, mid-refactor, Claude Code consults it as
a tool instead of carrying it:

```text
> refactor the telemetry module to comply with the vendor spec

⏺ I'll check the spec's exact requirements before touching the code.

⏺ cag - ask_document (MCP)
  question: "What payload fields, types and units does the vendor require
             for telemetry events, and what timestamp format?"
  ⎿ Events require device_id (string), ts_utc (ISO-8601 with milliseconds),
    seq (monotonic int) and readings[] using unit-suffixed keys — temp_c,
    load_pct, volt_mv. Unknown keys are rejected, not ignored (§4.2).
    [doc 3 vendor-spec.pdf · cache: memory · evaluated 38 of 41,772 prompt tokens · 590 ms]

⏺ Unit-suffixed keys with strict rejection — renaming the fields in
  telemetry.py and adding a schema guard…
```

The 41,772-token spec was evaluated **once**, weeks ago, on your hardware. This
turn cost the cloud model ~40 tokens of question and ~100 of answer (plus a few
hundred tokens of tool definitions, once per session — honesty in accounting).
The spec itself never occupies the agent's context: not this session, not after
compaction, not next month. The same shape works as
a **second brain**: pin your notes, your research corpus digest, or a project's
design doc, and any MCP-capable agent — coding or otherwise — gets a private,
grounded, queryable memory that never inflates its context and never leaves
your machine.

## Running it as a dedicated chatbot

The original motivation for this project — a narrow-domain support bot — is a
configuration, not a fork. Two things matter:

**Retrieval is not a dial here.** The model *always* has the entire document in
context — there is no top-k retrieval step that can miss. What `temperature`
controls is only the **wording** of the generated answer:

- **Razor-sharp extractive mode** (support/compliance bots): send
  `"temperature": 0` per request — or set it stack-wide with
  `DEFAULT_TEMPERATURE=0.0` in `.env` — for deterministic, quote-like answers.
  Same question → same answer, every time.
- **Hybrid mode** (assistant-style): the default (`0.2`) keeps answers grounded
  but lets the model synthesize and phrase naturally across the whole document —
  full context *plus* room to compose. Raise toward `0.7` only if you want
  looser prose; grounding still comes from the system rule.

**The guardrail is the system prompt**, not luck: every query is wrapped in
*"answer using only the information in the document; if it's not there, say so
plainly"* (the `SYSTEM_TEMPLATE` constant in `api/app/cag.py` — edit it there
if your bot needs a persona or a refusal style, then rebuild; existing caches
re-warm themselves on next use). Wire your bot's frontend to the n8n webhook
(`POST /webhook/cag/query`, with `history` for multi-turn), cap
`DEFAULT_MAX_ANSWER_TOKENS` if you want terse replies, and that's the whole
deployment.

## Choosing a model (state of play, mid-2026)

Set `LLAMA_MODEL` in `.env` to any GGUF on Hugging Face (`repo[:quant]`),
then `python llamacag.py stop && python llamacag.py start`:

| Model | Context | Download | Fits comfortably in | Notes |
|-------|---------|----------|---------------------|-------|
| `google/gemma-4-12B-it-qat-q4_0-gguf` *(default)* | 262k | 6.5 GB | 16 GB RAM | Google's official QAT build — Q4 with near-full quality, Apache 2.0 |
| `google/gemma-4-E4B-it-qat-q4_0-gguf` | 128k+ | ~3 GB | 8 GB RAM | Edge-class Gemma 4, lightest sensible option |
| `unsloth/Qwen3.5-9B-GGUF:Q4_K_M` | 256k+ | ~5.5 GB | 16 GB RAM | Strongest small dense alternative |
| `google/gemma-4-26B-A4B-it-qat-q4_0-gguf` | 256k | ~15 GB | 32 GB RAM | MoE: 26B-class answers at ~4B-active speed — best quality-per-second on big-RAM CPU boxes |
| `ggml-org/GLM-4.7-Flash-GGUF:Q4_K` | 202k | ~27 GB | 64 GB RAM | Workstation class |

Two knobs matter alongside the model:

- **`LLAMA_CTX_SIZE`** (default **65536**) — total context, split across slots;
  each document must fit `ctx ÷ slots − 1024`. KV memory scales with it, but
  **`LLAMA_CACHE_TYPE_KV=q8_0`** (the default) halves that at negligible
  quality cost — this is why 64k is now an affordable default.
- **`CAG_SLOTS`** (default **1**) — how many documents stay *hot in RAM* at
  once. With 2–4 slots, alternating between documents never touches disk;
  llama-server divides the context between them.

**Changing model or quant invalidates existing caches**; they self-heal
(recompute + re-save) on their next query.

### GPU & native acceleration

To be clear about intent: **CPU is the universal floor, not the design point.**
The stack runs anywhere Docker does, but the fast path — and the original
design target — is accelerated inference: Metal on Apple Silicon (unified
memory is ideal for CAG, since model *and* KV caches share one big pool), CUDA
on NVIDIA, Vulkan on Intel/AMD. Pick your lane:

- **Apple Silicon (Metal):** Docker Desktop on macOS has **no GPU passthrough**,
  so run llama-server natively (`brew install llama.cpp`) and keep the rest of
  the stack in Docker: `python llamacag.py start --native-llama` prints the
  exact host command and the two `.env` lines that point `cag-api` at it. Full
  recipe in [docs/HARDWARE.md](docs/HARDWARE.md).

- **NVIDIA:** `python llamacag.py start --gpu` (CUDA image; NVIDIA Container
  Toolkit required — bundled with Docker Desktop on Windows).
- **Intel Arc / AMD:** `python llamacag.py start --vulkan` — passes `/dev/dri`
  through, so it works on **Linux hosts**. Docker Desktop's VM has no `/dev/dri`;
  on Windows laptops (including Intel Arc iGPUs) run the CPU stack — a modern
  multi-core CPU handles the 4–12B class fine, and CAG means the expensive
  prompt processing happens only once per document anyway.

## Configuration

Everything lives in `.env` (created by `setup`; see [.env.example](.env.example)
for all knobs and comments). The defaults are sensible; the ones you might touch:

| Variable | Default | |
|----------|---------|---|
| `LLAMA_MODEL` | `google/gemma-4-12B-it-qat-q4_0-gguf` | Model to download & serve |
| `LLAMA_CTX_SIZE` | `65536` | Total context (split across slots) |
| `CAG_SLOTS` | `1` | Documents kept hot in RAM simultaneously |
| `LLAMA_CACHE_TYPE_KV` | `q8_0` | KV cache precision (`f16` to disable quantization) |
| `LLAMA_EXTRA_ARGS` | — | Extra llama-server flags, e.g. `--cache-reuse 256` |
| `DOCUMENTS_FOLDER` | `./documents` | Folder watched by the ingestion workflow |
| `GENERIC_TIMEZONE` | `UTC` | Used by n8n schedules |
| `N8N_PORT` / `CAG_API_PORT` / `LLAMA_PORT` / `DB_PORT` | `5678/8000/8080/5432` | All bound to `127.0.0.1` |

## Updating & maintenance

Honest answer: **almost none, and no code changes for new models.**

- **New model comes out?** Edit one line in `.env` (`LLAMA_MODEL=<hf-repo:quant>`)
  and restart. The model tables in this README / [docs/HARDWARE.md](docs/HARDWARE.md)
  are *suggestions*, not dependencies — they go stale cosmetically, never
  functionally. The only exception: a brand-new model *architecture* needs
  llama.cpp support first, which you get with `docker compose pull` (fresh
  llama-server image), still zero code changes here.
- **Updating the stack itself:** `git pull`, then
  `docker compose pull && python llamacag.py start` (compose rebuilds cag-api).
  CI on every commit is the regression gate.
- **What's automated already:** the nightly maintenance workflow reconciles
  disk and database; caches invalidated by a model switch heal themselves on
  next query. Postgres stays pinned to a major version; n8n updates when you
  pull and migrates its own database.
- **The one watch-item:** llama-server's slot save/restore API is marked
  experimental upstream. If it ever changes, only `api/app/llama.py` follows —
  and even mid-breakage, queries stay correct via the self-heal path (they just
  get slower until fixed).

## Troubleshooting

- **`status` shows llama-server unreachable right after start** — it's
  downloading the model. `python llamacag.py logs llama-server` shows progress.
- **Ingest returns 413 (document too large)** — the error includes the measured
  token count; raise `LLAMA_CTX_SIZE` in `.env` and restart, or split the file.
- **Everything is slow / OOM on Windows** — Docker Desktop's WSL2 VM defaults to
  half your RAM. The default model + 64k context wants ~10 GB in the VM; give it
  that (Settings → Resources, or `.wslconfig`), or lower `LLAMA_CTX_SIZE`, or
  switch to the E4B model.
- **Dropped a file but nothing happened** — is the *CAG Document Ingestion*
  workflow activated in n8n? Executions tab shows each attempt and any error.
  Warming a big document on CPU takes minutes: check `GET /documents` status.
- **First query after a restart is slower than the rest** — that's the one-time
  disk restore of the KV cache (still far cheaper than re-reading the document).
  If you see `cache_source: "recomputed"`, the cache file was lost/invalid and
  was rebuilt automatically.

## Project layout

```
├── api/                    # cag-api: FastAPI orchestrator (registry, slots, self-healing)
│   ├── app/                #   config / db / extract / llama client / cag engine / routes
│   └── tests/              #   unit + API tests (pytest, no services needed)
├── mcp/                    # cag-mcp: MCP server exposing the stack to Claude Code / agents
│   ├── cag_mcp/            #   FastMCP app + tools (server.py) + cag-api client (client.py)
│   └── tests/              #   tool tests over a MockTransport fake of cag-api
├── database/               # schema (documents, query_log) + n8n DB bootstrap
├── docs/                   # PRD.md and ARCHITECTURE.md — start there for design
├── n8n/workflows/          # 3 importable workflows: ingestion, query, maintenance
├── docker-compose.yml      # llama-server + cag-api + n8n + postgres
├── docker-compose.gpu.yml  # NVIDIA (CUDA) override
├── docker-compose.vulkan.yml # Intel/AMD GPU override (Linux hosts)
└── llamacag.py             # setup / start / stop / status / logs / query
```

## Upgrading from v1

v2 is a rebuild, not an upgrade — v1's KV caches never worked (its scripts
called llama.cpp flags that don't exist) so there is nothing to migrate. If you
ran v1: `docker compose down -v` on the old checkout, pull, then follow the
quick start. The full reasoning is in [docs/PRD.md](docs/PRD.md) and
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Development

```bash
pip install -e "./api[dev]"
pytest api            # no Docker needed
ruff check api

pip install -e "./mcp[dev]"   # the MCP server
pytest mcp            # fakes cag-api over httpx MockTransport
ruff check mcp
```

CI runs lint, tests, workflow JSON validation, and a compose config check on
every push.

## Acknowledgements

- [llama.cpp](https://github.com/ggml-org/llama.cpp) — inference, and the slot
  save/restore API that makes honest CAG a config option instead of a science project
- [n8n](https://n8n.io/) — the automation layer
- [AbelCoplet/llama-cag-n8N](https://github.com/AbelCoplet/llama-cag-n8N) — the
  original this rebuild descends from

## License

[MIT](LICENSE)
