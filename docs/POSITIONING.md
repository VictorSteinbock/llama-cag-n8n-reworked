# Positioning — why this exists, why it got no traction, and the pivot

**Last updated:** 2026-07-02

## 1. The traction post-mortem (honest version)

The original (March 2025) got ~zero traction for five compounding reasons, none
of them "the idea was bad":

1. **The core didn't work.** The bash scripts called llama.cpp flags that never
   existed. Anyone who tried it bounced within minutes, and nobody stars a repo
   that fails its own quick start.
2. **It read as the wrong category.** The README said "chat with your documents
   locally" — a category owned by polished one-click apps (AnythingLLM, Open
   WebUI, LM Studio). In that comparison it loses instantly on install
   friction, UI, and maturity. Its actual category — persistent KV-cache
   document memory — was buried under implementation detail.
3. **Install friction vs. the neighbors.** Compile llama.cpp on your Mac, edit
   env files, import workflows, versus competitors' single installer. Every
   extra step is a funnel drop.
4. **Zero distribution.** Never announced anywhere — no r/LocalLLaMA post, no
   n8n community thread, no Show HN. GitHub search does not surface small
   projects; links do. No links, no visitors, no stars.
5. **Repo hygiene signals.** A fork (excluded from GitHub search, banner
   pointing at a stale parent), no license (legally unusable), no CI badge, no
   visuals above the fold at first.

v2 already fixed 1, 3, and 5. This document is the plan for 2 and 4.

## 2. The 2026 landscape (checked, not assumed)

- **AnythingLLM / Open WebUI / LibreChat** own "self-hosted chat UI with
  document workspaces". All are **RAG systems**: chunk → embed → vector DB →
  retrieve per query. Excellent at big corpora; approximate by design.
- **LM Studio / Ollama / Jan** own "run a local model easily". They cache
  prompts in RAM per session; nothing document-pinned, nothing that survives a
  restart.
- **Persistent KV-cache reuse** is productized in the *cloud* (Anthropic/
  OpenAI/Gemini prompt caching) and active in *research* (persistent
  edge KV caches, recomputation-free caching), but **none of the mainstream
  self-hosted tools ship it as a feature**.

That last line is the whole game: **"your document, read exactly once, with the
model's memory of it saved to disk" has no self-hosted incumbent.**

## 3. What this project actually is

> **A local document-memory engine.** Feed it a document once; the model's
> internal state (KV cache) is persisted to disk. Every question afterwards —
> today, tomorrow, after a reboot — skips re-reading and evaluates only your
> question. Exact, not approximate: the model always sees the *entire*
> document, not retrieved chunks.

Three consumers of one engine:
- **API** (`cag-api`) — for anything programmatic.
- **n8n workflows** — drop-a-file automation, query webhook, maintenance.
- **LlamaCag UI** (sibling repo) — the desktop control room: chat, documents,
  stack health, model switching.

## 4. Who it's for / not for

**For:** the person with a handful of dense reference documents (manuals,
contracts, rulebooks, specs, theses) who asks repeated questions over weeks on
consumer hardware, and wants automation hooks (n8n) around it.

**Not for (say it out loud, it builds trust):** thousand-document knowledge
bases — that's retrieval territory; use AnythingLLM/Open WebUI. Documents
bigger than the context window. Teams needing multi-user auth.

## 5. Message architecture

- **Tagline:** *Read once. Ask forever.*
- **Sub:** Local CAG stack — llama.cpp KV caches persisted to disk, orchestrated
  by a typed API, automated with n8n, fully offline after one model download.
- **Proof beat, not adjective beat:** the README must show the timings JSON —
  first query evaluates ~30,000 tokens, the next one evaluates ~40 — and the
  restart-survival claim. Numbers are the marketing.
- **Comparison section:** "When you should use AnythingLLM instead" — honest
  two-way table. Credibility with the r/LocalLLaMA crowd comes from knowing
  your lane.

## 6. Launch checklist (when the visuals land)

1. README top: hero graphic, tagline, CI badge, 2-command quick start, timing
   proof, comparison section. Screenshots of LlamaCag UI once rebuilt.
2. Repo metadata: topics (`llama-cpp`, `n8n`, `cag`, `kv-cache`, `local-llm`,
   `self-hosted`, `document-qa`), description, website→UI repo link.
3. Posts (each shows the timings screenshot + hero):
   - r/LocalLLaMA — "I built a local stack where the model reads your document
     exactly once (persistent KV cache + n8n automation)". Lead with numbers.
   - n8n community forum — automation angle: folder-drop → queryable webhook.
   - Show HN — only after a stranger has successfully quick-started.
4. Cross-link both repos ("engine ↔ desktop control room"), credit the
   original AbelCoplet repos as ancestry.
5. Cut a `v2.0.0` release with a changelog — releases are a discovery surface.

## 7. Pivot verdict

No pivot of substance is needed — the niche was right and is still open. The
pivot is **presentational**: stop competing as a chat app, start owning
"persistent document memory for local models", prove it with numbers in the
first screenful, and actually tell people it exists.
