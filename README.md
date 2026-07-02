<p align="center">
  <img src="docs/images/hero.svg" alt="llama-cag-n8n — Read once. Ask forever." width="100%">
</p>

# llama-cag-n8n

**Ask questions about your documents with a fully local LLM — without the model
re-reading the document every time.**

[![CI](https://github.com/VictorSteinbock/llama-cag-n8n-reworked/actions/workflows/ci.yml/badge.svg)](https://github.com/VictorSteinbock/llama-cag-n8n-reworked/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-34D399.svg)](LICENSE)

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
tokens. **Numbers, not adjectives — that's the whole point.**

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

One engine, three ways to drive it:

- **This stack** — the engine (`llama-server`) + a typed API (`cag-api`) +
  n8n automation. Everything programmatic or folder-/webhook-driven lives here.
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
2. Import the three workflows from [`n8n/workflows/`](n8n/workflows/)
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

### GPU acceleration

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
pytest api            # 32 tests, no Docker needed
ruff check api
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
