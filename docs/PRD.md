# PRD — llama-cag-n8n v2

**Status:** Adopted · **Last updated:** 2026-07-02
**Supersedes:** v1 (March 2025, no PRD existed)

> **Historical record.** This is the v2.0 PRD as adopted (July 2026) — the
> rationale-of-record for the rebuild, kept as written. Two things have moved
> on since: the project's *headline* pivoted from "chat with your documents"
> to the **trust/verification layer** (`POST /verify`, calibration, the agent
> grounding gate — see the [README](../README.md) and
> [ROADMAP](ROADMAP.md) F1–F5, F11), and the shipped default context is now
> **65536**, not the 32k mentioned in the risks table. Note also that feature
> ids here are the PRD's own numbering, not ROADMAP's F-series (PRD F6 = cache
> self-heal; ROADMAP F6 = the `prepare` CLI).

## 1. Problem

Answering repeated questions about the same reference documents with a local LLM is
wasteful: the model re-reads (re-processes) the full document on every query. For a
30k-token manual on CPU hardware that is minutes of redundant prompt processing per
question.

Cache-Augmented Generation (CAG) fixes this by computing the model's KV cache for a
document **once**, persisting it to disk, and restoring it for every subsequent query so
the model only has to process the question itself.

v1 of this project attempted this with hand-rolled bash scripts around the `llama.cpp`
CLI. The flags it relied on never existed, the Docker design mounted host-compiled macOS
binaries into Linux containers, and the n8n workflows referenced services that were never
defined. v2 is a rebuild on primitives that exist and are maintained upstream.

## 2. Goals

- **G1 — True CAG:** Document KV caches are computed once, persisted to disk, and reused
  across queries *and* container restarts.
- **G2 — Fully local:** No external API required at any point (model download from
  Hugging Face happens once at first boot; everything after runs offline).
- **G3 — Cross-platform:** One command brings the stack up on Windows, macOS, or Linux.
  The only host dependencies are Docker and Python 3.10+ (stdlib only, for the helper CLI).
- **G4 — Automation via n8n:** Drop a file in a folder → it becomes queryable. Query via
  a stable webhook. Nightly maintenance keeps disk usage honest.
- **G5 — Operable:** Health endpoints, a query log, and a status command make it obvious
  whether the system works and why it doesn't.

## 3. Non-goals

- **Not a RAG system.** No embeddings, no vector store, no retrieval ranking. Documents
  must fit in the model's context window; that is the deliberate trade-off of CAG.
- **Not multi-tenant / not internet-facing.** Single user, loopback-bound ports, no
  auth. Hardening for public exposure is out of scope.
- **No GPU cluster orchestration.** Single-node only; optional single-GPU (NVIDIA CUDA)
  via a compose override.
- **No custom inference code.** All inference concerns (templating, caching, slot
  persistence) are delegated to `llama-server`.
- **No document OCR.** Text-based PDF, TXT, MD, and HTML only. Scanned PDFs are out of scope.

## 4. Users

A single technical user (developer / power user) self-hosting a private
"ask-my-documents" service on their own machine, orchestrating automations in n8n.

## 5. Functional requirements

| ID | Requirement |
|----|-------------|
| F1 | Ingest a document (TXT, MD, HTML, text-based PDF) via HTTP upload or by dropping it into a watched folder. |
| F2 | On ingest: extract text, count tokens with the model's real tokenizer, reject documents that don't fit in `ctx_size − answer_reserve`, warm the KV cache, persist it to disk, record metadata in Postgres. |
| F3 | Deduplicate ingests by content hash (re-dropping the same file is a no-op). |
| F4 | Answer a question against a chosen document (`document_id`) or, by default, the most recently cached document. |
| F5 | On query: restore the document's KV cache into the inference slot if it isn't already active; only the question and answer tokens are newly processed. |
| F6 | Self-heal: if a cache file is missing/corrupt, fall back to recomputing the prefix (slow but correct) and re-persist it. |
| F7 | List and delete documents (deleting removes DB row, cache file, and frees the slot). |
| F8 | Log every query (question, answer, token counts, duration, success) to Postgres. |
| F9 | Maintenance endpoint: remove orphaned cache files, flag documents whose cache file disappeared, report disk usage. |
| F10 | n8n workflows: folder-watch ingestion, query webhook (`POST /webhook/cag/query`), nightly maintenance — importable with **zero** credential setup. |
| F11 | Helper CLI: `setup` (generate `.env` with real secrets), `start [--gpu]`, `stop`, `status`, `logs`, `query`. |

## 6. Non-functional requirements

- **N1 — Warm-once economics:** For a document of *N* tokens, queries after the first
  must process ~(question + answer) tokens, not ~(N + question + answer). Verifiable via
  the `timings` block returned by llama-server.
- **N2 — Resilience:** Every service restarts `unless-stopped`; the API degrades
  gracefully (reports which dependency is down) instead of crashing.
- **N3 — Security:** No shell execution anywhere in the request path. Parameterized SQL
  only. Ports bound to `127.0.0.1`. Secrets generated, never defaulted.
- **N4 — Footprint:** Default model (Gemma 4 12B QAT ≈ 6.5 GB) + 64k context with q8_0
  KV quantization runs in ~10 GB of Docker memory; the `.env` model table offers a ~3 GB
  edge model for 8 GB machines. Context size, slot count, and KV precision are `.env` knobs.
- **N5 — Maintainability:** API is a small typed FastAPI package with unit tests and CI
  (lint + tests) on every push.

## 7. Success criteria

1. `python llamacag.py setup && python llamacag.py start` → healthy stack on a clean
   machine with Docker (verified on Windows 11).
2. Drop `manual.pdf` into `documents/` → within one polling interval it appears as
   `cached` in `GET /documents`.
3. First query after restart restores the cache from disk; llama-server `timings`
   confirm the document prefix was **not** re-evaluated.
4. `curl -X POST .../webhook/cag/query -d '{"question": "..."}'` returns an answer with
   no credential or manual wiring beyond importing the workflows.
5. CI is green: ruff, pytest, workflow JSON validation, `docker compose config`.

## 8. Key decisions & trade-offs

| Decision | Rationale | Trade-off accepted |
|----------|-----------|--------------------|
| `llama-server` (official image) instead of CLI + bash scripts | Slot save/restore, prompt caching, HF model download, and an OpenAI-compatible API are maintained upstream; v1's approach was rewritten there years ago | Coupled to llama-server's HTTP contract |
| Keep a thin Python service (`cag-api`) between n8n and llama-server | One place for registry, slot orchestration, extraction, self-healing; keeps n8n workflows trivial and credential-free | One more container |
| Chat-completions endpoint with a constant system message per document | Model-agnostic (server applies each model's template); byte-identical prefix ⇒ KV reuse works for any model | A few boilerplate template tokens re-evaluated per query |
| Default model: Gemma 4 12B official QAT GGUF (2026) | Un-gated Apache 2.0, 262k context, Q4 with QAT (near-full quality), single-file `-hf` download | 6.5 GB download; `.env` swap for smaller/bigger machines |
| KV cache quantization `q8_0` + flash attention (auto) by default | Halves KV memory ⇒ 64k context affordable on consumer RAM; negligible quality impact | `f16` escape hatch kept for paranoid setups |
| `CAG_SLOTS` parallel slots with LRU assignment (default 1) | Up to N documents stay hot in RAM; switching between them is free instead of a disk restore | Context divides across slots; per-document size limit shrinks accordingly |
| Serialized inference (engine-level lock) | Deterministic memory use; CPU inference is throughput-bound anyway | No concurrent generation |
| Postgres for metadata (2 tables) | Already required by n8n; enables dedupe, audit, stats | — |
| Documents must fit in a slot's context | This is CAG, not RAG | Big corpora need the (removed) chunking complexity of v1 — explicitly out of scope |

## 9. Risks

| Risk | Mitigation |
|------|------------|
| llama-server slot API changes (it is marked experimental upstream) | All calls isolated in `api/app/llama.py`; self-heal path (F6) keeps the system correct even if save/restore breaks |
| KV cache files are large (can exceed model size for long contexts) | Nightly maintenance + `DELETE /documents`; disk usage reported |
| n8n 2.x node churn | Workflows use only long-stable nodes (Webhook, HTTP Request, Schedule Trigger, Local File Trigger, Read/Write File, Set); no Code, no Execute Command |
| Docker Desktop memory limits on Windows (WSL2 defaults) | Documented; default context lowered to 32k; clear 413 error when a document doesn't fit |
