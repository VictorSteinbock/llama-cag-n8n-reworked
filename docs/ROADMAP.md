# Roadmap & implementation plans

This document is the backlog for llama-cag-n8n, written so that **you or a
contributor can pick up any item and execute it without further context.** Each
feature has a self-contained plan: what it unlocks, what it touches, the exact
steps, the tests to add, the invariants it must not break, and a "done when"
bar.

It exists because the two deep design reviews (see git history and
[docs/POSITIONING.md](POSITIONING.md)) produced a clear shortlist: a handful of
small, high-value core upgrades — chiefly ones that make the **grounding oracle**
honest and trustworthy — plus a few larger items that are real reworks and
should be *decided*, not drifted into.

Status legend: **Ready** (specified, no blockers) · **Ready·dep** (blocked only
on another roadmap item) · **Design-first** (needs a design decision before code).

| # | Feature | Tier | Effort | Status |
|---|---------|------|--------|--------|
| F1 | Quote-grounding check (`/verify` endpoint) | Core upgrade | S | Shipped (main) |
| F1b | MCP `verify` tool | Core upgrade | XS | Shipped (main) |
| F2 | Answer-gating pattern + fail-safe gate | Composition | S | Shipped (main) |
| F3 | Scope/conditions field in the verdict schema | Core upgrade | XS | Shipped (main) |
| F4 | Per-canon reliability battery (calibration) | New capability | M | Shipped (main) |
| F5 | Usage & cost-savings observability (`/stats`) | New capability | M | Shipped (main) |
| F6 | Document preprocessing (PDF→Markdown) helper | Tooling | M | Shipped (main) |
| F7 | Cross-document queries (concat / diff / federate) | Rework | L | Design-first |
| F8 | Multi-user / RBAC | Product fork | XL | Design-first |
| F9 | Zero-install web UI (served at `/ui`) | New capability | M | Shipped (main) |
| F10 | Sample documents + guided first-run | Tooling | S | Shipped (main) |
| F11 | Agent grounding gate (`integrations/`: cag_gate + Hermes + OpenClaw) | New capability | M | Shipped (main) |
| F12 | Async ingest (202 + status poll; non-blocking MCP) | Core upgrade | M | Ready |
| F13 | Auto-generated calibration battery | Tooling | S | Ready |
| F14 | Per-document prompt boundary marker (hostile-canon hardening) | Core upgrade | S | Design-first |
| F15 | Deferred DB columns (`cache_source`, `reliability`) + capability probe | Follow-up | S | Ready |

Still gating the **v2.1 tag** (not a feature): the Phase-4 live-model
verification run from
[IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) — everything above is verified
with fakes, offline browsers, and CI; one real boot with the default model
closes the loop.

---

## Invariants every change must respect

These are the load-bearing rules from [CLAUDE.md](../CLAUDE.md) and
[docs/ARCHITECTURE.md](ARCHITECTURE.md). Breaking one silently breaks caching or
security, so read them before touching `api/`:

1. **The system message for a document is byte-identical across warm and every
   query** (`SYSTEM_TEMPLATE` in `api/app/cag.py`). Never make it depend on the
   question, the schema, or history. KV prefix reuse — the entire point — dies
   the moment it varies. New features add *user-turn* content or *sampling*
   constraints, never system-prefix content.
2. **`json_schema` constrains sampling only.** It rides in `response_format`, not
   in the prompt text. Any verdict schema you add follows this path.
3. **No shell in the request path; parameterized SQL only.** Preprocessing tools
   that shell out (F6) live in the CLI / offline, never inside `cag-api`.
4. **Queries must stay correct with no cache files.** The self-heal path
   (`_make_hot` → recompute → deferred `_resave`) is the safety net; never make a
   missing `.bin` an error.
5. **Two-lock discipline.** `_lock` serializes slot *use* (assign + restore +
   completion are atomic); `_slots_guard` is a momentary micro-lock for the slot
   map so `health()` never queues behind a generation. Never hold `_slots_guard`
   across I/O.
6. **API changes are additive.** Add response fields; don't change or remove
   existing ones (n8n workflows, the MCP client, and LlamaCag UI all parse them).
   A DB column change needs a migration note (see F5).
7. **Model defaults live in three places that must agree**: `.env.example`, the
   `${VAR:-default}` fallbacks in all `docker-compose*.yml`, and
   `api/app/config.py`.

---

## Tier 1 — core upgrades that make the oracle honest

### F1 — Quote-grounding check (`POST /verify`)

**What & why.** Today the oracle returns `{claim, verdict, quote}`, and a human
or downstream node is trusted to check the quote. That leaves the trust gap the
reviews flagged: an LLM saying "supported" while *fabricating* the supporting
quote. This feature makes the citation a **mechanical check** — confirm the
returned quote actually appears in the source document — so a fabricated
citation is caught with zero LLM involvement. This is the single highest-value
item: it converts the marketing phrase "hash check for facts" into a real one,
and it is what the README's oracle section now points here for.

**What it catches / doesn't.** Hardens the two evidence-bearing verdicts
(`supported`, `contradicted` — both come with a passage that must exist).
Cannot harden `absent` (no quote by definition) and cannot verify *entailment*
(a real quote that doesn't actually support the claim). Those limits are stated
in the README and are the reason F4 (reliability) and the fail-safe gate (F2)
exist.

**Affected components.** New endpoint in `api/app/main.py`; new method in
`api/app/cag.py`; a pure helper module `api/app/grounding.py`; the
`claim-verification-workflow.json` retargets from `/query` to `/verify`
(simpler, and every consumer benefits). No change to `/query`, so nothing
downstream breaks. (The MCP follow-up shipped too: the server exposes a `verify`
tool — F1b.)

**Implementation steps.**
1. `api/app/grounding.py` (stdlib only — no new dependency): `def
   grounding(quote: str, content: str) -> dict` returning `{"grounded": bool,
   "match_ratio": float, "method": "exact"|"fuzzy"|"absent"}`. Normalize both
   sides (collapse whitespace, casefold). Empty quote → `{"grounded": None,
   "method": "absent"}`. Exact normalized substring → `grounded=True, ratio=1.0,
   method="exact"`. Otherwise slide a `difflib.SequenceMatcher` over
   document windows sized to the quote (± slack) and take the best ratio;
   `grounded = ratio >= settings.quote_match_threshold` (default `0.9`),
   `method="fuzzy"`. Keep it O(n) by only comparing windows near candidate
   anchor words, not every offset, so a 60k-token document stays fast.
2. `api/app/config.py`: add `quote_match_threshold: float = 0.9` (comment it as
   the paraphrase-tolerance dial that trades false alarms against missed
   fabrications).
3. `api/app/cag.py`: `def verify_claim(self, claim, document_id=None, ...)`.
   Build the verification prompt (`Verify strictly against the document: "<claim>".
   Give your verdict and the exact supporting or contradicting passage.`), call
   the existing `query(...)` path at `temperature=0` with the fixed verdict
   `json_schema` (reuse the schema shape from the README's Structured-output
   example, plus F3's `conditions` once that lands). Parse the JSON answer,
   run `grounding(parsed["quote"], doc_content)`, and return
   `{claim, verdict, quote, quote_grounded, match_ratio, document, timings}`.
   Load the document content the same way `query` does (so the doc is the hot
   one and the check runs against the exact bytes in the cache). **Invariant:**
   this reuses `query`'s message construction — do not fork the system message.
4. `api/app/main.py`: `POST /verify` with a `VerifyRequest {claim: str,
   document_id: int | None, max_tokens?, }`. Reuse the existing exception
   handlers (404/409/502 already map). If the model returns non-JSON despite the
   schema (shouldn't, but defend), return a `verdict="error"` object rather than
   a 500.
5. Retarget `n8n/workflows/claim-verification-workflow.json`: the HTTP node
   posts to `http://cag-api:8000/verify` with `{claim, document_id}` instead of
   assembling the `/query` schema by hand; the "Collect Verdict" Set node passes
   through the new `quote_grounded` field. Re-validate with the CI workflow
   check.

**Tests to add** (`api/tests/test_grounding.py` + extend `test_cag.py`,
`test_api.py`):
- exact quote present → `grounded True, method exact`.
- honest paraphrase within threshold → `grounded True, method fuzzy`.
- fabricated quote absent from doc → `grounded False` (the catch).
- `absent` verdict (empty quote) → `grounded None`.
- endpoint happy path + 404 on unknown `document_id` + non-JSON model answer →
  `verdict="error"`, not 500. (Extend `FakeLlama.chat` to return a canned JSON
  string; the fake document content is already available via the fake DB.)

**Risks / invariants.** Byte-identical prefix (step 3 reuses `query`). Fuzzy
threshold is a real knob — document that too strict inflates human review, too
loose lets near-miss fabrications pass. Keep the window search bounded so long
canons don't make `/verify` slow.

**Done when.** `/verify` returns a grounded verdict; a fabricated-quote test
proves the catch; the workflow uses it; `ruff check --no-cache api` +
`pytest api -q` green.

---

### F2 — Answer-gating pattern + fail-safe gate

**What & why.** The reviews found that for *support-bot-style* gating (a question
exists), decomposing a draft into claims is the wrong architecture — it verifies
facts but misses reasoning/conclusion errors. The better pattern: ask the oracle
the **original question** fresh (grounded, temp 0) and compare it to the draft.
One generation, no decomposition, catches conclusion errors. This item ships
that as a **documented pattern plus an optional `answer-gate` workflow**, and
codifies the fail-safe rule: auto-pass only on `supported`-with-grounded-quote;
route everything else to review.

**Affected components.** A new `n8n/workflows/answer-gate-workflow.json`; a new
section in the README ("Gating a support bot's answers") under the oracle;
no core change beyond F1 (the gate calls `/verify`).

**Implementation steps.**
1. Workflow `answer-gate`: webhook `POST cag/answer-gate` with `{question,
   draft, document_id?}`. Node A → `/query {question, temperature:0}` to get the
   grounded reference answer G. Node B → `/verify` with `claim = "This answer is
   fully supported by the document: <draft>"`. A Set node applies the gate:
   `pass = verdict=="supported" AND quote_grounded==true`; output `{pass,
   verdict, quote, grounded_answer: G, reason}`. Error branch as in the other
   workflows. Validate with the CI check (all six workflows).
2. README: short subsection under the oracle with the curl example and the
   fail-safe rule stated once, linking here.

**Tests.** Workflow JSON validation only (no core code). If any scoring logic
proves to need code, move it into a `/verify`-style endpoint rather than an n8n
Code node (Code nodes are banned by convention).

**Done when.** The workflow imports, the gate blocks a draft that overstates the
document, and passes one the grounded answer supports.

---

### F3 — Scope/conditions field in the verdict schema

**What & why.** The entailment gap's most common real shape is a *conditional*:
doc says "refundable only if defective", claim says "refundable". Both
`supported` and `contradicted` are wrong. Adding a `conditions` field lets the
gate compare the claim's scope to the evidence's scope instead of proliferating
verdict labels.

**Affected components.** The default verdict schema in `verify_claim` (F1); the
README oracle example; the workflow's pass-through fields.

**Implementation steps.**
1. Extend the default schema to `{claim, verdict, quote, conditions}` where
   `conditions` is a string ("" when unconditional). Update the prompt to ask
   for it.
2. Surface `conditions` in `/verify`'s response and in the workflow's Set node.
3. README: add the field to the Structured-output example and one sentence on
   using it in the gate.

**Tests.** Extend the F1 tests: a conditional passage yields non-empty
`conditions`; an unconditional one yields "".

**Done when.** `conditions` flows end to end; a conditional-refund fixture
surfaces the condition.

---

## Tier 2 — new capabilities, buildable, high signal

### F4 — Per-canon reliability battery (calibration)

**What & why.** The honest limit of long-canon verification is that an `absent`
verdict can be a retrieval miss (lost-in-the-middle), and the miss rate grows
with document length. Instead of hand-waving, **measure it**: run a
known-answer Q/A battery against a freshly ingested document and report its
accuracy, so a user knows the expected escalation rate before trusting the
oracle on that canon. Uses only existing primitives; nobody else ships this.

**Affected components.** New endpoint `POST /documents/{id}/calibrate`; scoring
helper (reuse `grounding.py`'s fuzzy match, or exact/`in` for short answers);
optional `calibration` n8n workflow wrapping it. No change to existing paths.

**Implementation steps.**
1. `cag.py`: `def calibrate(self, document_id, qa: list[{question, expected}])`
   → for each, `query(question, temperature=0)`, score the answer against
   `expected` (normalized containment first, fuzzy ratio as tiebreak; a strict
   mode can require exact). Return `{n, correct, accuracy, misses:[{question,
   expected, got}]}`.
2. `main.py`: `POST /documents/{id}/calibrate {qa:[...]}` (cap list length; 404
   if the doc doesn't exist). Ground truth is caller-supplied — document that
   clearly; this measures *this canon under this model*, not the model in
   general.
3. Optional: a `calibration` workflow so it's runnable from n8n; and surface the
   last accuracy on `GET /documents` (add a nullable `reliability` column —
   **migration note**, see F5's migration guidance).
4. README: a short "Know your canon's reliability" note under the oracle,
   pointing at the escalation-rate framing.

**Tests.** engine-level with the fakes: a fake that returns the expected answer
for 2 of 3 questions → `accuracy≈0.67`, one miss listed. Endpoint 404 + cap.

**Done when.** Calibrate returns a score and the miss list; documented as the
way to pick a safe (model × canon-size) operating point.

---

### F5 — Usage & cost-savings observability (`GET /stats`)

**What & why.** The receipt is per-query; there's no aggregate view. `query_log`
already stores `n_prompt_tokens`, `n_cached_tokens`, `n_eval_tokens`,
`duration_ms`. A `/stats` endpoint turns that into the demo-worthy story:
tokens served from cache (the work *not* redone), eval-vs-cached ratio,
queries/day, p50/p95 latency, and an optional cost-savings estimate against a
configurable cloud price.

**Affected components.** New `GET /stats` in `main.py` + aggregation in
`db.py`. No schema change in the **no-migration** version.

**Implementation steps.**
1. `db.py`: `def usage_stats(self)` — SQL aggregates over `query_log`: sum of
   `n_cached_tokens` (tokens reused), avg `n_eval_tokens`, count and duration
   percentiles over 24h/7d/all. Parameterized, read-only.
2. `config.py`: `cloud_price_per_1k_input: float = 0.0` (0 disables the money
   line). Savings ≈ `cached_tokens/1000 × price` — clearly labelled an estimate.
3. `main.py`: `GET /stats` returns the aggregates + savings. Extend
   `llamacag.py status` to print a one-line summary.
4. **Optional follow-up (needs migration):** add a `cache_source` column to
   `query_log` to show the memory/disk/recomputed distribution. Migration
   guidance: `database/schema.sql` only runs on a *fresh* volume; existing
   deployments need `ALTER TABLE query_log ADD COLUMN cache_source text;` — ship
   it as `database/migrations/00x_*.sql` and note it in "Updating &
   maintenance". Do the no-migration version first.

**Tests.** `db` stub-driven aggregation shape; endpoint returns the fields; a
price of 0 omits the money line.

**Done when.** `/stats` returns usage + savings; `status` shows the one-liner.

---

### F6 — Document preprocessing (PDF→Markdown) helper

**What & why.** The single biggest real-world ingestion gap (and the pain from
the v1 lineage): PDFs with charts, scans, or complex tables extract badly, and
the stack then trusts wrong text. The README now documents the *pattern*
(convert to Markdown first). This item adds an **optional, isolated helper** so
it's one command, without violating the shell-free request-path rule.

**Design constraint (important).** Conversion is **not** in `cag-api` — it shells
out to a converter and/or calls a vision model, which the request path forbids.
It lives in the CLI / an offline step. Ingestion still only accepts faithful
text; this just produces that text.

**Implementation steps.**
1. `llamacag.py prepare <file> [--out file.md]`: detect type; for a text-layer
   PDF, extract (reuse `pypdf`); for image/complex PDFs, call a converter. Keep
   the converter **pluggable** via `.env` (`PREPARE_CMD="marker {in} {out}"` or a
   vision-model endpoint) so no heavy dependency is forced on users who don't
   need it. If no converter is configured and the PDF has no text layer, print a
   clear message pointing at the recommended tools (marker / docling / a vision
   model) rather than failing opaquely.
2. Write the resulting `.md` to the watch folder (or `--out`), so the existing
   ingestion path picks it up unchanged.
3. Docs: expand the README "Preparing documents" subsection with the command and
   the pluggable-converter env var; note the privacy trade-off (a cloud vision
   converter sends the document out — a local vision model keeps it in).

**Tests.** CLI unit test: text-layer PDF → Markdown file written; missing
converter + image PDF → clear guided error (monkeypatch the converter call).

**Done when.** `llamacag.py prepare scanned.pdf` yields a `.md` ready to ingest,
with a configurable converter and an honest fallback message.

---

## User experience & accessibility

A design review of the "make it feel like an app" question found that the gap is
**not** "no friendly face exists" — the desktop control room
([LlamaCag UI](https://github.com/VictorSteinbock/LlamaCagUI): drag-drop upload,
chat with cache-source badges, document library, stack health/control, model
switching, dark theme, toasts, welcome onboarding) already covers most of it.
The two real gaps are: it requires *installing a second app*, and the sharpest
feature (the oracle) has *no GUI at all*. F9 and F10 close both.

### F9 — Zero-install web UI (served at `/ui`)

**What & why.** The lowest-friction face for a non-technical user: run
`python llamacag.py start`, open a URL, and drag in a document, chat, and verify
claims — with nothing to install. It complements the other faces rather than
replacing them: LlamaCag UI is the native power-user control room, n8n is
automation, and this is the casual daily-driver front door. It's also where the
**oracle finally gets a GUI** (paste claims → verdict table) and where
**residency becomes visible** (which documents are Hot / on Disk / Cold).

**Why served by cag-api, not a separate container.** Mounting a static
single-page app inside the existing API is the lightest feasible option: **no
new service, no new runtime dependency** (Starlette's `StaticFiles` ships with
FastAPI; `python-multipart` is already installed for uploads), and — because the
page is served from the same origin it calls — **no CORS to configure.** One
`start`, one URL.

**Security boundary (state it plainly).** The stack is unauthenticated by design;
loopback is the security boundary. The web UI is therefore for the **local host**
by default. Reaching it from a phone or another machine means binding the port
beyond `127.0.0.1`, which exposes an *unauthenticated* API on your network —
only do that behind a reverse proxy with auth, or on a trusted LAN you control.
General multi-user access is the F8 fork, not this.

**Feasibility — verified (11/11).** A vertical-slice harness mounted a static SPA
on the *real* `create_app()` and drove every tab's data path through the
`TestClient`: `/ui` serves `text/html` same-origin (no CORS); `StaticFiles` +
`python-multipart` are already present (no new dependency); multipart upload
ingests and warms; `GET /documents` carries the table fields; `GET /health`
`hot_documents` drives Hot/Disk/Cold; `/query` returns the `cache_source` + token
receipt; and a `json_schema` verdict parses cleanly into a table row. Only the
frontend HTML/CSS/JS remains — effort, not risk. (Real LLM JSON generation is
llama-server's job, verified separately against upstream docs, not re-tested here.)

**Affected components.** New `api/app/webui/` (a self-contained `index.html` +
inline CSS/JS, matching the established dark amber-on-slate palette); a one-line
mount in `api/app/main.py`. **No change to any existing endpoint** — the SPA is
pure client of `/documents`, `/query`, `/health`, `/maintenance`. Optional
`WEBUI_ENABLED` flag (config + the three-places rule) if you want it opt-out.

**Implementation steps.**
1. `api/app/webui/index.html`: a no-build single-page app (vanilla JS is enough;
   keep it dependency-free and self-contained like the SVGs). Tabs: **Chat**,
   **Library**, **Verify**, **Stats**.
2. Mount it: `app.mount("/ui", StaticFiles(directory=<webui dir>, html=True))`.
   Same-origin as the API ⇒ no CORS.
3. **Chat**: document picker (`GET /documents`, cached only), input box, send →
   `POST /query`; render the answer with the `cache_source` badge and the token
   receipt. Keep `history` client-side for multi-turn.
4. **Library**: `GET /documents` table; file input **and** drag-drop →
   `POST /documents` (multipart) with an "uploading… warming…" indicator that
   polls `GET /documents` until status flips to `cached`; delete → `DELETE`.
   Show **Hot / Disk / Cold** by cross-referencing `GET /health` `hot_documents`
   (slot → doc id) against the list.
5. **Verify**: a textarea (one claim per line) → per claim `POST /query` with the
   verdict `json_schema` at `temperature 0` (or `POST /verify` once **F1** lands,
   which also gives you the `quote_grounded` column) → a verdict table with
   colored chips. This is the oracle's first GUI.
6. **Stats**: `GET /health` (status, slots, hot docs) now; the cumulative
   "compute saved" line lights up once **F5** ships `GET /stats`.
7. README quick start: add "open http://localhost:8000/ui"; a screenshot.

**Alternative if you'd rather write Python than JS.** A Gradio (or Streamlit) app
in a `webui/` container reaches parity faster but costs a new service, a new
dependency, extra RAM, and cross-origin calls to the API. Prefer the static SPA
for footprint and cohesion; reach for Gradio only if hand-writing the frontend
is the blocker.

**Tests.** Add an API smoke test that `GET /ui/` returns `200 text/html`. The
underlying endpoints are already covered; a Playwright click-through is optional
and can come later.

**Done when.** `start` → `http://localhost:8000/ui` → drag a document, watch it
warm, chat, and verify a claim list — all in a browser, no install.

### F10 — Sample documents + guided first-run

**What & why.** Kill the empty-state cliff. A first-time user with nothing
ingested should reach a real answer in under a minute. Ship a couple of curated
sample documents and a one-click "try a sample" path so the "aha — it remembers"
moment happens before any of their own files are involved.

**Affected components.** A new `samples/` folder (1–2 short `.md` files — e.g. a
fake product manual and a one-page policy, chosen to show off extraction,
grounding, and the oracle); a "Try a sample" affordance in F9's web UI (and,
optionally, LlamaCag UI's empty state); one line in the README quick start.

**Implementation steps.**
1. `samples/acme-widget-manual.md`, `samples/refund-policy.md` — small, dense,
   self-contained, with a few checkable facts (numbers, conditions) so the
   Verify tab has something to catch.
2. Web UI empty state: a "Try a sample" button that `POST`s the sample text via
   `/documents/text` and drops the user into Chat with a suggested question.
3. README: mention the samples in the "Use it" block.

**Tests.** Trivial: the sample files parse as valid Markdown; the ingest of a
sample returns `cached` (covered by existing ingest tests with a fixture).

**Done when.** A fresh stack → one click → a sample is cached and answerable,
including a Verify example that shows a `contradicted` catch.

### What we're deliberately not building

- **A Tauri/Electron rewrite.** The native-desktop-app box is already checked by
  LlamaCag UI (PySide6, 69 tests). Rebuilding it in web tech trades a working,
  tested app for a smaller bundle — a lateral move, not a win.
- **Cloud sync / accounts / monetization.** These contradict the local-first,
  "never leaves your machine" contract that is the whole point. If teams ever
  become the goal, that is the F8 fork — a deliberate decision with its own
  roadmap, not a feature bolted on here.
- **PDF first-page thumbnails.** Real rendering cost for little value in a
  text-first tool; the document list already shows what matters (name, tokens,
  status, residency).
- **In-answer source highlighting.** The model doesn't return character offsets,
  so "highlight the exact sentence" isn't reliably implementable. The oracle's
  quote field (hardened by **F1**) is the honest, buildable version of "show me
  where this came from."

## Shipped since the original list

### F11 — Agent grounding gate (`integrations/`)

Shipped on main (see [docs/AGENTS.md](AGENTS.md) for the design and
[`../integrations/`](../integrations) for the code): the framework-agnostic
`cag_gate` package (fail-safe `GroundingGate` with the fabricated-quote check
and the **evidence floor** `min_grounded_quote_chars`, 15 unit tests), the
Hermes Agent plugin (`cag_verify`/`cag_ask`/`cag_remember` tools, reactive
`post_tool_call` tripwire, `CAG_OVERRIDE_MEMORY=1` hard gate,
`CAG_ABSENT_TO_MEMORY=1` episodic-memory tagging), the OpenClaw `cag-verify`
skill (stdlib-only, fail-closed exit codes), and a CI job. Anything still
listed below is *not* part of this.

## Post-audit backlog (added 2026-07-04)

Three items born from the external hardening + utility audits. Same contract as
above: executable without further context.

### F12 — Async ingest (202 + status poll)

**What & why.** Ingest warms synchronously — deliberate ("warm-at-ingest": the
first question is never the slow one) but on CPU a big document blocks the HTTP
call, and the MCP `ingest_file` call, for **minutes**. In Claude Code that looks
like a hang; the utility audit ranked it a top adoption blocker.

**Sketch.** Additive only — the default stays synchronous. `POST /documents`
gains `?mode=async`: insert the row (`status=pending`), return **202** with
`{id, status: "pending"}`, then run the existing warm through the engine's
deferred-work runner (`_spawn`, same pattern as the self-heal re-save) and mark
`cached`/`failed` as today. Dedupe check stays *before* the 202 (an identical
re-drop still returns the existing row immediately). MCP `ingest_file` uses
async mode and returns "warming — check `list_documents` in a few minutes"; the
web UI already polls `GET /documents`, so it needs nothing.

**Invariants.** Warm still serializes under `_lock` (the background thread
queues like any other slot user); no new state machine — `pending → cached |
failed` already exists in the schema; API additive (202 only when explicitly
requested).

**Tests.** Fakes: async ingest returns 202 + pending; after the deferred runner
fires, the row is `cached` and a query works; dedupe of an in-flight document
returns the existing row; failure path marks `failed` with the error.

**Done when.** MCP ingest of a large document returns in under a second and the
document becomes queryable on its own a few minutes later.

### F13 — Auto-generated calibration battery

**What & why.** F4's honest cost is authoring the known-answer battery by hand
(hours, and it blocks the compliance persona). Let the stack draft it: the model
reads the canon it will be measured on and proposes the Q/A pairs; a human
approves.

**Sketch.** `POST /documents/{id}/calibrate/generate {n}` → one `query()` at
`temperature 0` with a `json_schema` for
`{"items": [{"question": ..., "expected": ...}]}` asking for *n* short,
unambiguous, answer-in-the-text pairs spread across the document. Return them
for **human review** — never auto-run into `/calibrate`. Cap `n` at
`CALIBRATE_MAX_ITEMS`.

**Honest limit (document it).** Same-model circularity: a self-drafted battery
measures *recall stability* (lost-in-the-middle, format drift) — exactly what
`absent`-rate calibration needs — but it cannot measure whether the model
misreads the text, because the drafter and the examinee are the same model.
Human review of the pairs is the mitigation, and it's still 10× faster than
authoring.

**Tests.** Fakes: generate returns the schema shape; `n` over the cap → 422
naming the knob; a generated battery round-trips into `calibrate()`.

**Done when.** Draft battery in one call, human trims it, `/calibrate` scores it.

### F14 — Per-document prompt boundary marker (Design-first)

**What & why.** `SYSTEM_TEMPLATE` wraps content in `<document>` tags; a hostile
document containing `</document>` can pose as instructions beyond its boundary.
Documented today as a trust boundary ("the canon is trusted input" — README);
this item would harden it: an unguessable per-document boundary id generated at
ingest, stored on the row, used in the wrapper (`<document id="…">…</document
id="…">`) so content cannot forge its own closing tag.

**Why design-first, not code.** (a) The system message must stay byte-identical
per document — a *stored* nonce satisfies that, but the template change
invalidates **every existing cache once** (self-heal absorbs it: one slow query
per document). (b) The threat model is single-operator ingesting their own
documents; the payoff is real mainly for third-party/untrusted canons. Decide
whether that trade is worth the churn before building.

### F15 — Deferred DB columns + startup capability probe

**What & why.** Two optional columns were deliberately **cut** from the v2.1
build (a `hasattr` check is not DB tolerance — unmigrated deployments would
500): `query_log.cache_source` (memory/disk/recomputed distribution in `/stats`)
and `documents.reliability` (surface the last calibration score in
`GET /documents`). Shipping them needs the full kit: migrations
`database/migrations/001_cache_source.sql` + `002_reliability.sql`, a startup
capability probe (feature lights up only when the column exists), an "Updating &
maintenance" note, and additive API fields. The engine hook for reliability
already exists (`set_reliability` no-ops until the column lands).

**Done when.** A fresh deployment gets both features; an unmigrated one keeps
working with them dark.

## Tier 3 — reworks that need a decision first

### F7 — Cross-document queries (concat / diff / federate)

**Why it's here.** Several attractive use cases (compare two contract versions,
query across a "shelf", "what changed in v2.3") quietly need what the
architecture deliberately doesn't do: **a query targets one document.**

**The pragmatic near-term (document it now, no code):** if related documents fit
in one context window, concatenate them into a single canon (one file) — cross-
references then work for free. This covers a surprising amount of "multi-doc"
demand and should be written up as the first answer.

**The real feature (design-first):** true cross-document synthesis or diffing
needs one of:
- a `POST /query/multi {question, document_ids:[...]}` that fans out to each
  document, collects grounded answers, then synthesizes — but synthesis is a
  second inference over combined outputs, and it breaks the one-slot mental
  model; latency and the serialized lock make it a real design exercise; or
- a diff mode that queries the same claim against two documents and compares
  verdicts (this one is mostly F1 run twice + a compare — the *tractable* slice;
  spec it as `POST /diff {claim, document_id_a, document_id_b}` returning both
  grounded verdicts side by side).

**Decision needed:** ship only the tractable slices (concat pattern + claim
diff), or take on federated synthesis (larger, changes the model). Recommend the
former; leave synthesis to the agent layer (an MCP client can already call
`ask_document` across several documents and combine — that's orchestration, not
a core feature).

### F8 — Multi-user / RBAC

**Why it's here.** Real demand (teams), but it changes what the product *is*:
today it's single-user, loopback, no-auth by design. Adding accounts, per-user
document scoping, and access control is a **product fork**, not a feature.

**What it would take (sketch, for the decision):** an auth layer in front of
`cag-api` (the API stays internal; a gateway does authn/z); an `owner`/`acl`
column on `documents` and scoping on every query; n8n credentialing (which the
zero-credential workflow design currently and deliberately avoids); and a threat
model, since "loopback only" stops being the security boundary.

**Decision needed before any code:** does this stay a personal/self-hosted tool
(keep it lean) or become a team tool (a different roadmap, different
positioning)? Don't drift into it — decide it. Until then, the honest answer in
"Why not just Open WebUI" stands: teams needing multi-user auth should use a
RAG-first product.

---

## How to contribute one of these

1. Open an issue naming the feature (F#) so work isn't duplicated.
2. Follow the plan; respect the invariants above.
3. `ruff check --no-cache api mcp integrations` clean; `pytest api -q`,
   `pytest mcp -q`, and `pytest integrations -q` green; workflow JSON validated
   by the CI check; `docker compose config -q` passes if you touched compose.
4. Keep API changes additive and update the three-places-must-agree config if
   you added a knob.
5. Update the relevant docs (README section, this file's status) in the same PR.
