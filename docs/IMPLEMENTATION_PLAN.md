# Implementation Plan — oracle hardening + zero-install web UI

This document is the **build-ready** consolidation of the roadmap's Ready features
([docs/ROADMAP.md](ROADMAP.md) F1–F6, F9, F10), deepened from medium specs into
plans a multi-agent build can execute directly. It is written to be **human-reviewed
first, then executed on a single feature branch** (`feat/oracle-hardening-and-webui`)
by parallel agents, and finally merged to `main` as one coherent capability jump.
Every feature here is grounded in the actual code paths it touches — the file/line
anchors, function signatures, and response shapes are read from the tree, not assumed.
Read [CLAUDE.md](../CLAUDE.md) and [docs/ARCHITECTURE.md](ARCHITECTURE.md) before
touching `api/`; the binding invariants below are load-bearing.

## 2. Executive summary

Two themes, eight features. **Make the oracle honest** (F1, F3, F2, F4, F5) turns the
grounding oracle from a slogan into a mechanical check: `POST /verify` confirms a
returned quote actually occurs in the source bytes (F1), a shared verdict schema gains
a `conditions` scope field so conditionals aren't mislabeled (F3), an answer-gate
workflow gates a support bot's draft against the canon with a fail-safe default (F2),
a per-canon reliability battery quantifies the escalation rate before you trust a
document (F4), and `GET /stats` finally aggregates the compute-saved story (F5).
**Give it a face** (F6, F9, F10) ships an offline PDF→Markdown `prepare` CLI (F6), a
zero-install web UI at `/ui` where the oracle gets its first GUI and residency becomes
visible (F9), and two curated sample documents with a one-click first-run so the "aha —
it remembers" moment lands in under a minute (F10).

**Headline wins:** the oracle becomes trustworthy (a fabricated citation is caught with
zero extra LLM calls), and a non-technical user can `start` the stack, open a browser,
drag in a document, chat with it, and verify claims — with nothing installed.

**Total effort:** ~8–11 focused dev-days serialized; ~4–5 calendar-days across 3–4
parallel agent lanes. No new runtime dependency is added by any feature; every change is
additive to the API; the KV-cache-reuse invariant is respected by construction.

## 3. Scope

**In scope** (this build): **F1** quote-grounding + `POST /verify` · **F3** `conditions`
scope field · **F2** answer-gate workflow · **F4** calibration battery · **F5** usage &
cost-savings observability · **F6** `prepare` CLI · **F9** zero-install web UI · **F10**
samples + guided first-run.

**Explicitly out of scope:**

- **F7 — Cross-document queries.** A query targets one document by design; true
  cross-document synthesis/diffing is a *rework* that breaks the one-slot mental model
  and needs a design decision first (concat pattern + claim-diff are the tractable
  slices, deferred). Design-first in [docs/ROADMAP.md](ROADMAP.md).
- **F8 — Multi-user / RBAC.** Adding accounts, per-user document scoping, and an auth
  gateway is a *product fork* that ends the "loopback is the security boundary" model —
  a different roadmap and positioning, not a feature bolted on here.

## 4. Binding invariants checklist

Every feature section states which of these it respects and how. A PR is not mergeable
until a reviewer can tick each applicable box (N/A is valid when a feature provably
doesn't touch the surface).

1. **Byte-identical `SYSTEM_TEMPLATE`.** The system message for a document is
   byte-identical at warm and at every query (`SYSTEM_TEMPLATE`, `api/app/cag.py:37`).
   New behavior rides in a **user turn** or in **sampling** (`json_schema` / `temperature`,
   which `chat()` takes as arguments and never folds into a message), never in the system
   prefix. Any feature that generates must call the existing `query()` rather than build
   its own `messages`.
2. **No shell in the request path; parameterized SQL only.** No `subprocess` / `shell=True`
   under `api/app/`; every new SQL statement uses bound `%s` params.
3. **Queries stay correct with no cache files.** The self-heal path (`_make_hot` →
   recompute → deferred `_resave`) is the safety net; a missing `.bin` is never an error.
4. **Two-lock discipline.** `_lock` serializes slot *use*; `_slots_guard` is a momentary
   slot-map micro-lock, never held across I/O.
5. **API changes are additive.** Add endpoints and response fields; never change or remove
   existing ones (n8n, the MCP client, and LlamaCag UI parse them).
6. **Model/config defaults agree in three places.** Model/context/deployment knobs live in
   `api/app/config.py`, the `${VAR:-default}` fallbacks in all `docker-compose*.yml`
   `cag-api` blocks, and `.env.example`.
7. **`ruff` / `pytest` / workflow-valid / no-new-dep.** `ruff check` clean, `pytest api`
   + `pytest mcp` green, workflow JSON validates, `docker compose config -q` ×3 passes, and
   **no new runtime dependency** is added.

**n8n node whitelist:** webhook, httpRequest 4.2, set 3.4, splitOut, aggregate,
respondToWebhook, scheduleTrigger, localFileTrigger, readWriteFile, stickyNote. **No**
Code / Function / Cron / ExecuteCommand.

## 5. Table of contents

Feature sections are ordered by **build phase**, not feature number. The shared verdict
schema is defined **once** in F1 and referenced everywhere else.

- [Phase 0 — F1 & F3: mechanical quote-grounding + `POST /verify` (shared verdict schema)](#phase-0--f1--f3--mechanical-quote-grounding--post-verify-shared-verdict-schema)
- [Phase 1 — F5: usage & cost-savings observability (`GET /stats`)](#phase-1--f5--usage--cost-savings-observability-get-stats)
- [Phase 1 — F6: document preprocessing (`prepare` CLI)](#phase-1--f6--document-preprocessing-pdfscanscharts--markdown)
- [Phase 1 — F4: per-canon reliability battery (calibration)](#phase-1--f4--per-canon-reliability-battery-calibration)
- [Phase 2 — F2: answer-gating pattern + fail-safe gate](#phase-2--f2--answer-gating-pattern--fail-safe-gate)
- [Phase 2 — F9: zero-install web UI (served at `/ui`)](#phase-2--f9--zero-install-web-ui-served-at-ui)
- [Phase 3 — F10: sample documents + guided first-run](#phase-3--f10--sample-documents--guided-first-run)
- [Build sequence & dependency graph](#7-build-sequence--dependency-graph)
- [Testing & CI](#8-testing--ci)
- [Invariants & risk register](#9-invariants--risk-register)
- [Branch / PR / rollout strategy](#10-branch--pr--rollout-strategy)
- [For the reviewer — open decisions](#11-for-the-reviewer--open-decisions)

---

## 6. Feature sections (build-phase order)

### Phase 0 — F1 & F3 — mechanical quote-grounding + `POST /verify` (shared verdict schema)

**Goal & user value:** Today the oracle returns `{claim, verdict, quote}` and trusts a
human or downstream node to eyeball the quote — the exact trust gap the design reviews
flagged: a model can answer `supported` while *fabricating* the citation. F1 adds a new
`api/app/grounding.py` (stdlib `difflib` only) and `CagEngine.verify_claim()` behind
`POST /verify` that mechanically confirms the returned quote actually occurs in the
source bytes, turning "hash check for facts" into a real check with zero extra LLM
involvement. F3 extends the *one shared* verdict schema with a `conditions` string so the
most common entailment failure — a conditional ("refundable only if defective") answered
as unconditional — is surfaced as scope rather than mislabeled. They ship together because
they share the verdict schema, **defined once here** (`DEFAULT_VERDICT_SCHEMA`) and
referenced by every downstream feature.

**Effort & dependencies:** M (F1 S + F3 XS, co-built because they touch the same
schema/prompt/response/workflow). New standalone module + one engine method + one endpoint
+ one workflow retarget. **Shared surfaces:** reuses `query()`'s message construction
verbatim (invariant 1); reuses the existing exception handlers in `main.py` (404/409/502);
the verdict schema defined here is consumed by F2 (answer-gate `pass` rule), F4
(calibration reuses `grounding()`), and F9 (Verify tab). No dependency on other unbuilt F#.

**Files touched:**
- `api/app/grounding.py` **(new)** — pure `difflib` grounding helper.
- `api/app/config.py` **(modified)** — add `quote_match_threshold: float = 0.9`.
- `api/app/cag.py` **(modified)** — add `verify_claim()`, `DEFAULT_VERDICT_SCHEMA`,
  `VERIFY_PROMPT_TEMPLATE`; add `import json`.
- `api/app/main.py` **(modified)** — add `VerifyRequest`, `POST /verify`, list it in `index()`.
- `n8n/workflows/claim-verification-workflow.json` **(modified)** — retarget the HTTP node
  to `/verify`; pass through `quote_grounded`, `match_ratio`, `conditions`.
- `api/tests/test_grounding.py` **(new)** — unit tests for `grounding()`.
- `api/tests/test_cag.py` **(modified)** — engine-level `verify_claim` cases.
- `api/tests/test_api.py` **(modified)** — `POST /verify` contract cases.
- `api/tests/conftest.py` **(modified)** — extend `FakeLlama` with an `answer_json` mode.
- `docs/ROADMAP.md` **(modified)** — flip F1/F3 status to shipped; keep the specs.
- `README.md` **(modified)** — oracle example gains `conditions` + `quote_grounded`; the
  asymmetry paragraph.

**Interface / API changes:**

New endpoint (additive; `/query` unchanged):

```
POST /verify
  request  VerifyRequest { claim: str (min_length=1),
                           document_id: int | None = None,
                           max_tokens: int | None (ge=1, le=8192) = None }
  response { claim: str,
             verdict: "supported"|"absent"|"contradicted"|"error",
             quote: str,
             conditions: str,                 # F3: "" when unconditional
             quote_grounded: bool | None,     # None for absent / empty quote
             match_ratio: float,              # 0.0..1.0
             grounding_method: "exact"|"fuzzy"|"absent",
             document: { id, file_name, n_tokens },
             duration_ms: int,
             timings: { …same shape query() returns… } }
```

Status mapping (all via existing `main.py` handlers, lines 82–108): unknown `document_id`
→ **404** (`UnknownDocumentError`); no cached docs → **409** (`NoCachedDocumentError`);
llama down → **502** (`LlamaError`); validation → **422**. A model answer that is not
parseable JSON despite the schema → **200** with `verdict:"error"` (never a 500).

**The shared verdict schema** — placed in `cag.py` as `DEFAULT_VERDICT_SCHEMA`. This is
the single definition; F2/F4/F9 reference it by name, never redefine it:

```json
{
  "type": "object",
  "properties": {
    "claim":      { "type": "string" },
    "verdict":    { "enum": ["supported", "absent", "contradicted"] },
    "quote":      { "type": "string" },
    "conditions": { "type": "string" }
  },
  "required": ["claim", "verdict", "quote", "conditions"]
}
```

Config knob: `quote_match_threshold: float = 0.9` — paraphrase tolerance; higher = stricter
(more fabrications caught, more honest paraphrases flagged). Pure-Python behavioral knob, so
the three-places rule (invariant 6) does **not** apply — it is not a model/context/compose
knob. A one-line comment in `config.py` says why.

Workflow: `claim-verification-workflow.json`'s HTTP node "Verify Against CAG API" posts
`{claim, document_id}` to `http://cag-api:8000/verify` (no hand-assembled schema — this
**removes** the inline `{claim, verdict, quote}` schema currently at line 50 and lets the
server supply the 4-field `DEFAULT_VERDICT_SCHEMA`); the "Collect Verdict" Set node reads
`/verify`'s top-level fields directly and adds `quote_grounded`, `match_ratio`, `conditions`
pass-throughs; "Mark Failure" and "Aggregate Verdicts" gain the same fields. Node whitelist
respected (webhook / splitOut / httpRequest 4.2 / set 3.4 / aggregate / respondToWebhook /
stickyNote only).

**Implementation steps:**

1. **`api/app/grounding.py`** — stdlib `difflib` + `re` only:
   ```python
   import re
   from difflib import SequenceMatcher

   _WS = re.compile(r"\s+")

   def _normalize(text: str) -> str:
       return _WS.sub(" ", text).strip().casefold()
   ```
   `def grounding(quote: str, content: str, *, threshold: float = 0.9) -> dict:`
   - If `not quote or not quote.strip()`: return `{"grounded": None, "match_ratio": 0.0, "method": "absent"}`.
   - `nq = _normalize(quote); nc = _normalize(content)`.
   - If `nq in nc`: return `{"grounded": True, "match_ratio": 1.0, "method": "exact"}`.
   - Else fuzzy over **anchored windows** (fast on 60k tokens — never O(n²) over every offset):
     tokenize `nc` once with offsets (`words = list(re.finditer(r"\S+", nc))`, `qlen = len(nq)`);
     pick **anchor words** = distinct words in `nq` ≥ 4 chars (fall back to all `nq` words if
     empty); find content word-offsets whose token equals an anchor, capped at ~400 anchor hits;
     for each anchor char-offset `a` form a window `nc[start:end]` with `start = max(0, a - qlen//4)`,
     `end = min(len(nc), start + qlen + qlen//2)`; de-dup overlapping windows; run
     `SequenceMatcher(None, nq, window)` using `.real_quick_ratio()`/`.quick_ratio()` as cheap
     upper-bound filters before `.ratio()`; track `best`; return
     `{"grounded": best >= threshold, "match_ratio": round(best, 4), "method": "fuzzy"}`.
   - No candidate anchors → `best` stays `0.0` → `grounded=False, method="fuzzy"` (a quote sharing
     no ≥4-char word with the doc is not grounded — correct).

2. **`api/app/config.py`** — add under the existing fields:
   ```python
   # Paraphrase tolerance for POST /verify's mechanical quote check: the minimum
   # difflib ratio at which a non-exact quote still counts as grounded. Higher =
   # stricter. Behavioral-only (cag-api reads it; compose/geometry never share it),
   # so the three-places rule does NOT apply.
   quote_match_threshold: float = 0.9
   ```

3. **`api/app/cag.py`** — module-level constants (do **not** touch `SYSTEM_TEMPLATE`):
   ```python
   DEFAULT_VERDICT_SCHEMA = { ... }   # exactly the JSON above
   VERIFY_PROMPT_TEMPLATE = (
       'Verify this claim strictly against the document: "{claim}". '
       "Give your verdict (supported, contradicted, or absent), the exact verbatim "
       'supporting or contradicting passage as "quote" (empty string if absent), and in '
       '"conditions" any scope or condition the document places on the claim (empty '
       "string if it applies unconditionally)."
   )
   ```
   Add `import json`. New method:
   ```python
   def verify_claim(self, claim: str, document_id: int | None = None,
                    max_tokens: int | None = None) -> dict:
   ```
   - `question = VERIFY_PROMPT_TEMPLATE.format(claim=claim)`.
   - Call **the existing** `query()`:
     `result = self.query(question, document_id=document_id, max_tokens=max_tokens or self._settings.default_max_answer_tokens, temperature=0.0, json_schema=DEFAULT_VERDICT_SCHEMA)`.
     This is the whole invariant-1 safety: `query()` builds
     `messages = [self._system_message(...), *history, {"role":"user", ...}]` (cag.py:342–346)
     and passes `json_schema` to `chat()` only — the system prefix stays byte-identical to warm
     and to every other query. `verify_claim` **never** builds messages itself.
   - Re-fetch the document content to ground against the exact cached bytes: `query()` resolved
     which document answered (`result["document"]["id"]`); fetch
     `doc = self._db.get_document(result["document"]["id"], with_content=True)` and use
     `doc["content"]`. (Cannot read it from `query`'s return — `_document_response` never echoes
     content by design, main.py:186.)
   - Parse:
     ```python
     try:
         parsed = json.loads(result["answer"])
         verdict = parsed.get("verdict")
         quote = parsed.get("quote", "") or ""
         conditions = parsed.get("conditions", "") or ""
     except (json.JSONDecodeError, TypeError):
         verdict, quote, conditions = "error", "", ""
     if verdict not in {"supported", "absent", "contradicted"} and verdict != "error":
         verdict = "error"
     ```
   - Ground only when `verdict != "error"`:
     `g = grounding(quote, doc["content"], threshold=self._settings.quote_match_threshold)`;
     on `error`, `g = {"grounded": None, "match_ratio": 0.0, "method": "absent"}`.
   - Return, mirroring `query()`'s key shape so the MCP `_provenance` (reads
     `timings.cache_source`, `duration_ms`, `document`) and n8n keep working:
     ```python
     return {
         "claim": claim, "verdict": verdict, "quote": quote, "conditions": conditions,
         "quote_grounded": g["grounded"], "match_ratio": g["match_ratio"],
         "grounding_method": g["method"], "document": result["document"],
         "duration_ms": result["duration_ms"], "timings": result["timings"],
     }
     ```
   - Docstring states the asymmetry: grounding **hardens** `supported`/`contradicted` (a passage
     exists to check) but **cannot harden** `absent` (`quote_grounded=None`), and verifies
     *existence*, not *entailment*. Those limits are why F4/F2 exist.

4. **`api/app/main.py`** — add near `QueryRequest`:
   ```python
   class VerifyRequest(BaseModel):
       claim: str = Field(min_length=1)
       document_id: int | None = None
       max_tokens: int | None = Field(default=None, ge=1, le=8192)
   ```
   Add route (inside `create_app`, alongside `/query`):
   ```python
   @app.post("/verify")
   def verify(request: Request, body: VerifyRequest):
       return _engine(request).verify_claim(
           body.claim, document_id=body.document_id, max_tokens=body.max_tokens,
       )
   ```
   Add `"POST /verify {claim, document_id?, max_tokens?}"` to the `index()` endpoints list.
   **No new exception handlers** — `verify_claim` calls `query()`, which raises the same
   `UnknownDocumentError`/`NoCachedDocumentError`/`LlamaError` the existing handlers map.
   `verdict:"error"` is a normal 200 body, never raised.

5. **`n8n/workflows/claim-verification-workflow.json`** — HTTP node "Verify Against CAG API":
   set `url` to `http://cag-api:8000/verify`, keep `typeVersion` 4.2, `onError:"continueErrorOutput"`,
   the 3600000 timeout, and replace `jsonBody` with:
   ```
   ={{ JSON.stringify({ claim: $json.claim, document_id: $json.body.document_id }) }}
   ```
   (`$json.claim` is the split-out field from "Split Claims"; `$json.body.document_id` is the
   original webhook body carried through by `include: allOtherFields`.) "Collect Verdict" Set
   node now reads `/verify`'s top-level fields directly (no more `JSON.parse($json.answer)`):
   `claim` → `={{ $json.claim }}`, `verdict` → `={{ $json.verdict }}`, `quote` → `={{ $json.quote }}`,
   `conditions` → `={{ $json.conditions }}` (new), `quote_grounded` → `={{ $json.quote_grounded }}`
   (new, boolean), `match_ratio` → `={{ $json.match_ratio }}` (new, number),
   `cache_source` → `={{ $json.timings?.cache_source }}`, `error` → `={{ null }}`. "Mark Failure"
   adds the same new field names set to `null` so both branches produce identical shapes.
   "Aggregate Verdicts" `fieldToAggregate` list gains `conditions`, `quote_grounded`, `match_ratio`.
   Update the sticky note's response-shape line to
   `{claim, verdict, quote, conditions, quote_grounded, match_ratio, cache_source, error}`.

**Tests to add:**

- **`api/tests/test_grounding.py`** (new, no fakes — pure function):
  - `test_exact_substring_is_grounded_exact`: verbatim quote (mixed case + extra spaces) →
    `{"grounded": True, "match_ratio": 1.0, "method": "exact"}`.
  - `test_honest_paraphrase_within_threshold_is_fuzzy`: near-verbatim (one word changed) in a
    longer doc → `grounded True`, `method "fuzzy"`, `match_ratio >= 0.9`.
  - `test_fabricated_quote_absent_is_not_grounded`: quote about content not in the doc →
    `grounded False`, `method "fuzzy"`, `match_ratio < 0.9` (the catch).
  - `test_empty_quote_is_absent`: `grounding("", content)` and `grounding("   ", content)` →
    `{"grounded": None, "match_ratio": 0.0, "method": "absent"}`.
  - `test_threshold_is_respected`: same paraphrase, `threshold=0.99` → `grounded False`;
    `threshold=0.5` → `True`.
  - `test_large_document_stays_fast`: 60k-word synthetic doc, quote near the end; exact path
    returns, and a fuzzy call completes well under ~2 s (`time.monotonic()` guard on the
    anchored-window bound).
- **`api/tests/conftest.py`** (modified): `FakeLlama` gains `answer_json: str | None = None`;
  in `chat()`, `content = self.answer_json if self.answer_json is not None else self.answer`.
  Existing tests (which assert `answer == "the answer"`) are untouched (default `None`).
- **`api/tests/test_cag.py`** (modified, `engine`/`fake_llama`/`fake_db` fixtures):
  - `test_verify_grounded_supported`: ingest a doc containing "Fredville is the capital";
    `fake_llama.answer_json = '{"verdict":"supported","quote":"Fredville is the capital","conditions":"","claim":"..."}'`;
    assert `verdict=="supported"`, `quote_grounded is True`, `grounding_method=="exact"`,
    `match_ratio==1.0`, `conditions==""`, `document["id"]==1`.
  - `test_verify_catches_fabricated_quote`: `verdict:"supported"` with a `quote` absent from the
    doc → `quote_grounded is False`, `match_ratio < 0.9` (the catch, at the engine level).
  - `test_verify_absent_leaves_grounding_none`: `verdict:"absent", quote:""` →
    `quote_grounded is None`, `grounding_method=="absent"`.
  - `test_verify_surfaces_conditions`: `conditions:"only if the item is defective"` →
    `conditions=="only if the item is defective"` (F3 end-to-end at engine level).
  - `test_verify_non_json_answer_yields_error_verdict`: `answer_json = "sorry, I can't"` →
    `verdict=="error"`, `quote_grounded is None`, no exception.
  - `test_verify_reuses_query_prefix_byte_identical`: capture
    `fake_llama.last_messages[0]["content"]` from a plain `engine.query("hi")` (schema-less),
    then run `verify_claim`; assert the system message is byte-identical and
    `fake_llama.last_json_schema == DEFAULT_VERDICT_SCHEMA` and the last user turn starts with
    `Verify this claim strictly`. **(Guards invariant 1.)**
  - `test_verify_unknown_document_raises`: `engine.verify_claim("x", document_id=999)` →
    `pytest.raises(UnknownDocumentError)` (delegated from `query`).
- **`api/tests/test_api.py`** (modified, `client` fixture):
  - `test_verify_endpoint_happy_path`: ingest via `/documents/text`; `answer_json` = grounded
    verdict; `POST /verify {claim}` → 200 with `verdict`, `quote_grounded`, `conditions`,
    `match_ratio`, `grounding_method`, `document`.
  - `test_verify_unknown_document_is_404`; `test_verify_no_documents_is_409`;
    `test_verify_validation_is_422` (`{}` and `{claim:""}`);
    `test_verify_non_json_is_error_not_500` (200 with `verdict=="error"`);
    `test_verify_llama_outage_is_502` (`fake_llama.chat = boom` → 502).

**Invariants & risks:**
- **Invariant 1** — respected structurally: `verify_claim` never constructs messages; it calls
  `query()`, which folds the schema into sampling and puts the instruction in a *user* turn.
  `test_verify_reuses_query_prefix_byte_identical` pins it. Failure mode avoided: putting
  "verify this claim" into the system prompt would invalidate KV reuse for that doc on every
  query; the design forbids it by construction.
- **Invariant 2** — grounding is pure Python string work; the only DB call is
  `get_document(..., with_content=True)` (existing parameterized query). No process spawning.
- **Invariant 3** — inherits `query()`'s self-heal untouched; verification runs against
  `doc["content"]` (DB source of truth) regardless of cache state.
- **Invariant 5** — `/query` unchanged; `/verify` new; the workflow gains fields, drops none.
- **Invariant 7** — `difflib`/`re`/`json` are stdlib; no new dependency; workflow stays on
  whitelisted nodes.
- **Risk — fuzzy performance on 60k tokens:** mitigated by anchored windows + `quick_ratio()`
  pre-filter + anchor-hit cap; the exact-substring fast path is O(n). `test_large_document_stays_fast`
  guards it.
- **Risk — threshold miscalibration:** documented knob; tests pin behavior at 0.5/0.9/0.99.
- **Risk — over-claiming:** response and docs state the asymmetry plainly (hardens
  supported/contradicted; cannot harden `absent`; checks existence not entailment) so
  `quote_grounded=True` is never mistaken for "the claim is true."
- **Risk — model returns partial JSON:** `.get(..., "")` defaults + the `verdict:"error"`
  fallback keep the endpoint 200 and the shape stable; the schema's `required` list makes a
  compliant server populate all four fields.

**Acceptance (done when):**
- [ ] `api/app/grounding.py` exists; `grounding()` returns the three documented shapes; empty
  quote → `absent`; exact substring → `method "exact"`, ratio 1.0; fabricated quote → `grounded False`.
- [ ] `Settings.quote_match_threshold` defaults to `0.9` and drives `verify_claim`'s grounding call.
- [ ] `POST /verify` returns the full documented body; unknown doc → 404, empty stack → 409,
  llama down → 502, bad body → 422, non-JSON model answer → 200 `verdict:"error"`.
- [ ] `verify_claim` provably reuses `query()`'s byte-identical system prefix and sends
  `DEFAULT_VERDICT_SCHEMA` as the sampling schema (test asserts both).
- [ ] `DEFAULT_VERDICT_SCHEMA` includes `conditions` (required); a conditional fixture surfaces a
  non-empty `conditions`, an unconditional one yields `""`.
- [ ] `claim-verification-workflow.json` posts to `/verify`, passes through
  `quote_grounded`/`match_ratio`/`conditions` on both branches, and is valid on the CI check.
- [ ] `ruff check --no-cache api` clean; `pytest api -q` and `pytest mcp -q` green; no new dependency.
- [ ] README oracle example shows `conditions` + `quote_grounded` and states the grounding asymmetry.

---

### Phase 1 — F5 — usage & cost-savings observability (`GET /stats`)

**Goal & user value:** The per-query receipt (`cache_source`, evaluated-vs-cached tokens)
exists, but there is no aggregate view of what CAG has saved over time. `GET /stats` turns the
already-logged `query_log` columns into the demo-worthy story — tokens served from cache (work
*not* redone), eval-vs-reused ratio, queries/day, p50/p95 latency, and an optional cost-savings
estimate against a configurable cloud price — and `llamacag.py status` prints a one-line summary
so the payoff is visible from the CLI.

**Effort & dependencies:** M. No hard dependency on other F#. **Shared surfaces:** F9's Stats tab
consumes `GET /stats` (progressive — F9 works without it); establishes the "migration guidance"
precedent F4 also uses. Uses only existing `query_log` columns for the shipped version
(`n_cached_tokens`, `n_eval_tokens`, `duration_ms`, `created_at` — all present, `schema.sql:37–41`).

**Files touched:**
- `api/app/db.py` **(modified)** — add `Database.usage_stats()`.
- `api/app/config.py` **(modified)** — add `cloud_price_per_1k_input: float = 0.0`.
- `api/app/cag.py` **(modified)** — add `usage_stats()` engine wrapper (applies the price knob).
- `api/app/main.py` **(modified)** — add `GET /stats` route + list it in `index()`.
- `llamacag.py` **(modified)** — extend `cmd_status` with a one-line usage summary.
- `docker-compose.yml` **(modified)** — add `CLOUD_PRICE_PER_1K_INPUT` to the `cag-api` env block.
- `.env.example` **(modified)** — add commented `CLOUD_PRICE_PER_1K_INPUT` with guidance.
- `api/tests/conftest.py` **(modified)** — extend `FakeDatabase` with `usage_stats()`.
- `api/tests/test_db.py` **(modified)** — stub-driven aggregation-shape + savings tests.
- `api/tests/test_api.py` **(modified)** — `GET /stats` contract tests.
- `api/tests/test_cag.py` **(modified)** — engine-wrapper pricing test.
- **Optional follow-up (migration):** `database/migrations/001_cache_source.sql` **(new)**,
  `database/schema.sql` **(modified)**, `api/app/db.py` **(modified — `log_query` persists
  `cache_source`)**, `api/app/cag.py` **(modified — pass `cache_source` into `log_query`)**, README
  "Updating & maintenance" note. See the migration-ordering note in the build sequence.

**Interface / API changes:**
- **New endpoint** `GET /stats` → 200 JSON, read-only, no request body. Additive.
- Response shape (shipped / no-migration version):
  ```json
  {
    "windows": {
      "24h": {"queries": 0, "failed": 0, "tokens_reused": 0, "tokens_evaluated": 0,
              "avg_eval_tokens": 0.0, "reuse_ratio": 0.0,
              "p50_duration_ms": 0, "p95_duration_ms": 0},
      "7d":  { ...same keys... },
      "all": { ...same keys... }
    },
    "savings": {
      "cloud_price_per_1k_input": 0.0,
      "tokens_reused_all_time": 0,
      "estimated_usd": null,
      "is_estimate": true,
      "note": "Estimate: tokens_reused / 1000 * cloud_price_per_1k_input. Set CLOUD_PRICE_PER_1K_INPUT to your provider's input price to enable."
    }
  }
  ```
  When `cloud_price_per_1k_input == 0.0`, `savings.estimated_usd` is `null` — the money line is
  **present-but-null** (not omitted) so the additive contract holds and clients can branch on null.
  When `> 0`, `estimated_usd = round(tokens_reused_all_time / 1000 * price, 4)`.
- **Config knob:** `cloud_price_per_1k_input` (env `CLOUD_PRICE_PER_1K_INPUT`, default `0.0`) —
  registered in all three places (invariant 6).
- **Workflow nodes:** none. `index()` endpoint list gains `"GET /stats"`.

**Implementation steps:**

1. **`api/app/db.py` — `usage_stats()`.** One method, one static SQL string reused per window
   (Postgres `percentile_cont` is the percentile source and ignores NULL `duration_ms`):
   ```python
   _USAGE_WINDOW_SQL = """
       SELECT
           count(*)                                          AS queries,
           count(*) FILTER (WHERE NOT success)               AS failed,
           coalesce(sum(n_cached_tokens), 0)::bigint         AS tokens_reused,
           coalesce(sum(n_eval_tokens), 0)::bigint           AS tokens_evaluated,
           coalesce(avg(n_eval_tokens), 0)::float            AS avg_eval_tokens,
           coalesce(percentile_cont(0.5)  WITHIN GROUP (ORDER BY duration_ms), 0)::int AS p50_duration_ms,
           coalesce(percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms), 0)::int AS p95_duration_ms
       FROM query_log
       WHERE (%s::interval IS NULL OR created_at > now() - %s::interval)
   """

   def usage_stats(self) -> dict[str, Any]:
       def window(interval: str | None) -> dict[str, Any]:
           row = self._one(self._USAGE_WINDOW_SQL, (interval, interval)) or {}
           reused = row.get("tokens_reused") or 0
           evaluated = row.get("tokens_evaluated") or 0
           denom = reused + evaluated
           row["reuse_ratio"] = round(reused / denom, 4) if denom else 0.0
           return row
       return {"24h": window("24 hours"), "7d": window("7 days"), "all": window(None)}
   ```
   The `NULL`-interval branch makes the all-time window drop the time filter within the *same*
   static SQL — the interval is **bound** (`(interval, interval)`, psycopg positional params need
   it twice), never concatenated. `reuse_ratio` is computed in Python to keep the SQL uniform and
   dodge divide-by-zero.

2. **`api/app/config.py`** — add after `default_temperature`:
   ```python
   # Cost-savings estimate dial (GET /stats). A cloud provider's per-1k *input*-token
   # price; savings ≈ tokens_reused/1000 × this. 0.0 (default) hides the money line.
   cloud_price_per_1k_input: float = 0.0
   ```

3. **`api/app/cag.py` — engine wrapper** (keeps `Database.usage_stats()` pure aggregation; pricing
   policy lives where `Settings` do, mirroring how `list_documents`/`maintenance` delegate):
   ```python
   def usage_stats(self) -> dict:
       windows = self._db.usage_stats()
       price = self._settings.cloud_price_per_1k_input
       reused_all = windows["all"].get("tokens_reused") or 0
       estimated = round(reused_all / 1000 * price, 4) if price > 0 else None
       return {
           "windows": windows,
           "savings": {
               "cloud_price_per_1k_input": price,
               "tokens_reused_all_time": reused_all,
               "estimated_usd": estimated,
               "is_estimate": True,
               "note": ("Estimate: tokens_reused / 1000 * cloud_price_per_1k_input. "
                        "Set CLOUD_PRICE_PER_1K_INPUT to your provider's input price to enable."),
           },
       }
   ```

4. **`api/app/main.py`** — add below `/maintenance`:
   ```python
   @app.get("/stats")
   def stats(request: Request):
       return _engine(request).usage_stats()
   ```
   Add `"GET /stats"` to the `index()` `endpoints` list.

5. **`llamacag.py` — one-line status summary.** In `cmd_status`, after the health-check loop and
   before `print_resource_snapshot()` (llamacag.py:290), fetch `/stats` from the same `cag-api`
   base and print one line, guarded exactly like the health block so a stats hiccup never fails
   `status`:
   ```python
   api_base = f"http://localhost:{port(env, 'CAG_API_PORT', '8000')}"
   try:
       s_status, s_body = http_get(f"{api_base}/stats")
       if s_status == 200:
           stats = json.loads(s_body)
           day = stats["windows"]["24h"]; allw = stats["windows"]["all"]
           usd = stats["savings"]["estimated_usd"]
           money = f", ~${usd} saved" if usd else ""
           print(f"     usage: {day['queries']} queries/24h, "
                 f"{allw['tokens_reused']:,} tokens reused all-time{money}")
   except (OSError, json.JSONDecodeError, KeyError):
       pass  # stats are a nicety; never fail `status` over them
   ```
   The money clause appears only when `estimated_usd` is truthy (price configured).

6. **Config three-places sync.** Add to `docker-compose.yml` `cag-api.environment` (next to
   `DEFAULT_MAX_ANSWER_TOKENS`, line 53):
   ```yaml
       - CLOUD_PRICE_PER_1K_INPUT=${CLOUD_PRICE_PER_1K_INPUT:-0.0}
   ```
   Add to `.env.example` near the `DEFAULT_TEMPERATURE` block:
   ```bash
   # GET /stats cost-savings estimate. Your cloud provider's price per 1,000 INPUT
   # tokens (e.g. 0.003). Left at 0.0 the money line stays hidden; the figure is a
   # rough estimate (tokens reused × price), not a bill.
   #CLOUD_PRICE_PER_1K_INPUT=0.0
   ```
   The GPU/Vulkan compose overrides replace only the *llama* command block, not `cag-api` — so no
   edit is needed there, but **confirm at build** that no `docker-compose.*.yml` redefines
   `cag-api.environment` wholesale (base compose does not; the overrides target `llama-server`).

7. **Optional follow-up — `cache_source` distribution (needs migration).** Only after the
   no-migration version ships. This closes a real gap: `cag.py:349` computes `cache_source` at query
   time but `log_query` (cag.py:376, db.py:126) does **not** persist it.
   - `database/migrations/001_cache_source.sql` **(new)** — `ALTER TABLE query_log ADD COLUMN IF
     NOT EXISTS cache_source TEXT;` with a header explaining schema.sql runs only on a fresh volume.
   - `database/schema.sql` **(modified)** — add `cache_source TEXT,` to `query_log` after
     `duration_ms` so fresh volumes match.
   - `api/app/db.py` — `log_query` gains `cache_source: str | None = None`, appended to the INSERT
     column list and `params` tuple (**trailing** param, so the FK-retry `params[1:]` slice at
     db.py:158 keeps working).
   - `api/app/cag.py` — the success-path `log_query(...)` call passes `cache_source=cache_source`.
   - `usage_stats` window SQL gains
     `count(*) FILTER (WHERE cache_source = 'memory'|'disk'|'recomputed')` counts surfaced as
     `"sources": {"memory", "disk", "recomputed"}`. Pre-migration rows have `NULL cache_source` and
     fall outside all three filters (documented as "pre-migration queries").
   - README "Updating & maintenance": the migration is one-time, forward-only, non-destructive;
     `ADD COLUMN IF NOT EXISTS` is idempotent.

**Tests to add:**
- **`api/tests/test_db.py`** (stub-driven, `_one`-injection style):
  - `test_usage_stats_shapes_three_windows_and_reuse_ratio` — inject `db._one` returning a canned
    row `{tokens_reused: 900, tokens_evaluated: 100, ...}`; assert keys `"24h"/"7d"/"all"` and each
    window's `reuse_ratio == round(900/1000, 4) == 0.9`.
  - `test_usage_stats_reuse_ratio_zero_when_no_tokens` — `tokens_reused=0, tokens_evaluated=0` →
    `reuse_ratio == 0.0` (no ZeroDivisionError).
  - `test_usage_stats_binds_interval_twice_and_never_concatenates` — capture `params` per `_one`
    call; assert each is a 2-tuple with both elements equal (`("24 hours","24 hours")`,
    `("7 days","7 days")`, `(None, None)`).
- **`api/tests/conftest.py`** — `FakeDatabase.usage_stats()` returning the real `{24h,7d,all}` shape
  (sums over `self.queries`; documented that the fake collapses time windows since it logs no
  timestamp — window-differentiation is a live-DB concern out of scope for the fake).
- **`api/tests/test_api.py`**:
  - `test_stats_endpoint_returns_windows_and_savings` — ingest one text doc, run 2 queries,
    `GET /stats`; assert 200, `windows.all.queries == 2`, `windows.all.tokens_reused == 2 * 480`
    (the fake `chat` returns `cache_n: 480`, conftest.py:163), `savings.is_estimate is True`.
  - `test_stats_hides_money_line_when_price_zero` — default settings → `savings.estimated_usd is None`,
    `savings.cloud_price_per_1k_input == 0.0`.
  - `test_stats_shows_savings_when_price_set` — client built with `Settings(..., cloud_price_per_1k_input=0.003)`
    (local fixture) → after 1 query `estimated_usd == round(480/1000 * 0.003, 4)`.
  - `test_index_lists_stats_endpoint` — `GET /` includes `"GET /stats"`.
- **`api/tests/test_cag.py`**: `test_engine_usage_stats_applies_price` — assert the `savings` block
  wraps `Database.usage_stats()` output and applies the price from `settings` (pricing lives in the
  engine, not the DB fake).

**Invariants & risks:**
- **Invariant 2** — the interval is *bound* (`%s::interval`), never interpolated; `usage_stats` is
  read-only `SELECT` and touches only `query_log`, keeping cag-api's writer monopoly over
  `documents` intact.
- **Invariant 5** — new route, additive index string, money line present-but-null when disabled;
  `log_query`'s new `cache_source` (follow-up) is a trailing keyword-default param and trailing SQL
  column, so `params[1:]` FK-retry is unaffected.
- **Invariant 6** — `CLOUD_PRICE_PER_1K_INPUT` in `config.py` + `docker-compose.yml` `cag-api` env
  + `.env.example` together; `docker compose config -q` ×3 in CI enforces it.
- **Locks (4)** — untouched; `usage_stats` never touches `_lock`/`_slots_guard`.
- **No new dep (7)** — `percentile_cont` is stock Postgres; the CLI reuses `http_get` (urllib).
- **Failure modes handled:** empty `query_log` → every aggregate `coalesce`s to 0 and `reuse_ratio`
  guards divide-by-zero → clean all-zeros, never a 500; all-NULL `duration_ms` → `coalesce(..., 0)`
  yields 0; `/stats` fetch failing in `cmd_status` → wrapped `try/except`; `/stats` is a pure DB
  read so it answers even when inference is down (exactly when an operator wants historical numbers).
- **Noted (not a risk):** the fake collapses time windows (no timestamp); 24h-vs-7d-vs-all
  correctness is Postgres behavior tested only under a live DB — the stub test proves the interval
  is bound correctly.

**Acceptance (done when):**
- [ ] `GET /stats` returns `{windows:{24h,7d,all}, savings}` with the documented keys; empty log
  yields all-zeros without error.
- [ ] `Database.usage_stats()` is parameterized (interval bound, not concatenated) and read-only;
  `reuse_ratio` never divides by zero.
- [ ] `cloud_price_per_1k_input` present in `config.py`, `docker-compose.yml` (`cag-api` env), and
  `.env.example`; `docker compose config -q` ×3 passes.
- [ ] Price `0.0` → `estimated_usd is None`; price `> 0` →
  `estimated_usd == round(tokens_reused_all_time/1000 * price, 4)`.
- [ ] `python llamacag.py status` prints the one-line usage summary and still succeeds when
  `/stats` is unreachable.
- [ ] `FakeDatabase.usage_stats()` returns the real shape; new `test_db`/`test_api`/`test_cag` cases pass.
- [ ] `ruff check --no-cache api` clean; `pytest api -q` and `pytest mcp -q` green.
- [ ] (Optional follow-up) `database/migrations/001_cache_source.sql` idempotent; `schema.sql`
  matches; `cag.py` passes `cache_source` into `log_query`; `/stats` windows carry
  `sources:{memory,disk,recomputed}`; README documents the one-time migration.

---

### Phase 1 — F6 — document preprocessing (PDF/scans/charts → Markdown)

**Goal & user value:** Rich source documents — scanned PDFs, chart/diagram-heavy reports, complex
multi-column tables — extract badly or not at all through the `pypdf` text path (`extract._from_pdf`,
which raises 415 on a scan: "OCR is out of scope", extract.py:58), and the stack then trusts wrong
text as ground truth (the one failure no downstream oracle safeguard can catch). This ships
`python llamacag.py prepare <file>`, an offline CLI step that turns such documents into faithful
Markdown and drops it in the watch folder so the existing ingestion path picks it up unchanged. It
stays entirely out of `cag-api` so the request path remains shell-free.

**Effort & dependencies:** M. No dependency on other roadmap items. **Shared surfaces:** the
`llamacag.py` CLI (adds a `prepare` subcommand alongside setup/start/stop/status/logs/query) and the
`.env`/`.env.example` config surface (adds `PREPARE_CMD`, `PREPARE_OUT_FOLDER`). Downstream, F10's
samples and F9's Library benefit from clean `.md` but neither is required. It reuses the *concept* of
`api/app/extract.py::_from_pdf` (text-layer detection) but must **not import it** — see Invariants.

**Files touched:**
- `llamacag.py` **(modified)** — new `cmd_prepare`, `prepare` subparser, small helpers, `import shlex`.
- `.env.example` **(modified)** — new "Document preparation" block: `PREPARE_CMD`, `PREPARE_OUT_FOLDER`.
- `api/tests/test_prepare.py` **(new)** — CLI unit tests, placed under `api/tests/` so the existing
  `pytest api` job collects them; imports the repo-root `llamacag` module via a `sys.path` shim.
- `README.md` **(modified)** — expand the "Preparing documents (PDFs, scans, tables)" section.
- `docs/ROADMAP.md` **(modified)** — flip F6 status to reflect the shipped helper.

**Interface / API changes:** No HTTP endpoints, no `cag-api` change, no workflow nodes (CLI/offline
by design — the n8n whitelist is irrelevant here).
- **New CLI command:** `python llamacag.py prepare <file> [--out FILE] [--force]`.
  - `<file>`: PDF (or already-text `.md`/`.txt`/`.html`, passed through) to prepare.
  - `--out FILE`: explicit destination `.md`. Default `<PREPARE_OUT_FOLDER or DOCUMENTS_FOLDER>/<stem>.md`.
  - `--force`: overwrite an existing destination (default: refuse and tell the user).
  - Exit codes: `0` success; `1` guided error (no converter + no text layer, converter missing on
    PATH, converter failed, destination exists without `--force`, unreadable input).
- **New config knobs** (both optional; live only in `.env`/`.env.example`, read by the CLI — **not**
  added to `api/app/config.py`, because `cag-api` never reads them, so the three-places rule does not
  trigger):
  - `PREPARE_CMD` — converter command template, e.g. `PREPARE_CMD=marker {in} {out}`. `{in}`/`{out}`
    are substituted as whole argv elements, split with `shlex.split`, run via
    `subprocess.run([...])` with a **list** argv (**no `shell=True`**).
  - `PREPARE_OUT_FOLDER` — where prepared `.md` files land (default `DOCUMENTS_FOLDER`, the watch
    folder, so ingestion is automatic).

**Implementation steps:**
1. **Config plumbing.** `read_env()` (llamacag.py:44) already parses `.env` into a dict. In
   `cmd_prepare`: `env = read_env()`; `prepare_cmd = env.get("PREPARE_CMD", "").strip()`;
   `out_folder = PROJECT_ROOT / env.get("PREPARE_OUT_FOLDER", env.get("DOCUMENTS_FOLDER", "./documents"))`.
2. **Resolve source & destination.** `src = Path(args.file)`; `return 1` if it doesn't exist / isn't
   a file. `dest = Path(args.out) if args.out else (out_folder / (src.stem + ".md"))`. If
   `dest.exists() and not args.force`: print a `--force` hint and `return 1`. Ensure `dest.parent`
   exists.
3. **Already-text inputs pass through.** If `src.suffix.lower()` ∈
   `{".md",".markdown",".txt",".text",".html",".htm"}`, read and write to `dest` (normalizing to
   `.md`), print `[OK] Copied … (already text; no conversion needed)`, `return 0`.
4. **PDF text-layer detection — `_pdf_text_layer(path) -> str | None`.** Lazily `import` pypdf
   *inside the function* (it is an `api` dependency, not a root one — a bare clone may lack it). On
   `ImportError`, treat as "cannot self-extract" (fall through to the converter or guided error,
   never crash). Algorithm mirrors `extract._from_pdf` but is intentionally re-implemented (not
   imported): `reader = PdfReader(str(path))`;
   `text = "\n\n".join(p.extract_text() or "" for p in reader.pages).strip()`; catch `Exception`
   broadly (corrupt streams raise bare `KeyError`/`struct.error`, encrypted PDFs raise
   `DependencyError`) → "no usable text". Return the text if non-empty, else `None`.
5. **Decision tree in `cmd_prepare`:** (a) `text = _pdf_text_layer(src)`; if non-empty → write to
   `dest`, print `[OK] Extracted text layer … Review it, then it will be ingested from the watch
   folder.`, `return 0` (**text-layer path never shells out** — fastest, offline). (b) No text layer
   **and** `prepare_cmd` set → run the converter (step 6). (c) No text layer **and** `prepare_cmd`
   empty → guided error (step 7).
6. **Converter invocation — `_run_converter(prepare_cmd, src, dest) -> int`.** Substitute into a temp
   path so a failed converter leaves no half-written `dest`: `tmp_out = dest.with_suffix(".md.partial")`.
   `parts = shlex.split(prepare_cmd)`;
   `argv = [{"{in}": str(src), "{out}": str(tmp_out)}.get(tok, tok) for tok in parts]` (explicit token
   map — no string interpolation into a shell line). Guard the executable: `if shutil.which(argv[0])
   is None:` print a PATH message and `return 1`. `proc = subprocess.run(argv, cwd=PROJECT_ROOT,
   capture_output=True, text=True)` — **list argv, no `shell=True`**. On `returncode != 0`: print
   `stderr[:2000]`, unlink `tmp_out`, `return 1`. Handle both output styles: if `tmp_out` exists and
   non-empty → `tmp_out.replace(dest)`; elif `proc.stdout.strip()` → write stdout to `dest`; else
   print "produced no output" and `return 1`. Print
   `[OK] Converted … Review it before trusting the grounding.`, `return 0`.
7. **Guided error — `_no_converter_message(src)`.** Multi-line, naming concrete options:
   ```
   [!!] '<name>' has no extractable text layer (scanned, image-only, or chart/table-heavy),
        and no converter is configured. cag-api ingests faithful text only; turning a visual
        PDF into Markdown is a separate step. Configure one converter in .env as PREPARE_CMD:

          Local, private (document never leaves your machine):
            marker   — pip install marker-pdf ; PREPARE_CMD=marker {in} {out}
            docling  — pip install docling     ; PREPARE_CMD=docling {in} --to md --output {out}
            a local vision model (e.g. llama.cpp mmproj / Ollama) that emits Markdown

          Cloud vision model (FASTER/higher quality, but the document IS SENT to a third party —
          do not use for confidential material):
            your provider's PDF/vision-to-Markdown CLI as PREPARE_CMD=<cmd> {in} {out}

        Then re-run: python llamacag.py prepare "<path>"
   ```
   `return 1`.
8. **Argparse wiring** (in `main()`, mirroring the `p_query` block at llamacag.py:408):
   ```python
   p_prepare = sub.add_parser("prepare", help="convert a PDF/scan/chart doc to Markdown for ingestion")
   p_prepare.add_argument("file", help="path to the document to prepare")
   p_prepare.add_argument("--out", help="destination .md (default: watch folder / PREPARE_OUT_FOLDER)")
   p_prepare.add_argument("--force", action="store_true", help="overwrite an existing destination")
   p_prepare.set_defaults(func=cmd_prepare)
   ```
   Add `import shlex` (subprocess/shutil/Path already imported).
9. **README + `.env.example`.** Add the `PREPARE_CMD`/`PREPARE_OUT_FOLDER` block to `.env.example`
   (commented, with marker/docling/vision examples). Expand the README "Preparing documents" section:
   the exact command, the text-layer-vs-converter behavior, the pluggable knob, the privacy trade-off
   (a cloud vision converter sends the document out; a local vision model / marker / docling keeps it
   on your machine), and the review step: **always eyeball the produced `.md` before trusting grounding.**

**Tests to add** (`api/tests/test_prepare.py` — collected by `pytest api`; header shim
`import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))` then
`import llamacag`; `llamacag` is pure stdlib so it imports with zero extra deps):
- `test_prepare_text_layer_pdf_writes_markdown` — build a blank PDF via `PdfWriter().add_blank_page()`,
  `monkeypatch.setattr(pypdf._page.PageObject, "extract_text", lambda self, *a, **k: "Clause 4: refunds within 30 days")`
  (the pattern `test_extract.py` uses); point `PREPARE_OUT_FOLDER` at `tmp_path` (monkeypatch
  `read_env`). Call `llamacag.cmd_prepare(Namespace(file=<pdf>, out=None, force=False))`; assert return
  `0`, `(tmp_path/"<stem>.md")` contains `"refunds within 30 days"`, and **the converter was never
  invoked** (monkeypatch `subprocess.run` to a sentinel that fails the test if called — proves the
  text path is shell-free).
- `test_prepare_image_pdf_without_converter_is_guided_error` — blank PDF, `extract_text` → `""`;
  `read_env` returns `{}`; assert return `1` and stdout names `"marker"`, `"docling"`, `"vision"`, and
  `"PREPARE_CMD"`, and no `.md` written.
- `test_prepare_image_pdf_with_converter_runs_it` — no text layer;
  `read_env` returns `{"PREPARE_CMD": "fakeconv {in} {out}", "PREPARE_OUT_FOLDER": str(tmp_path)}`;
  monkeypatch `shutil.which` → truthy and `subprocess.run` → a fake that writes `"# Converted\nbody"`
  to the `{out}` path (parsed from the argv it receives) and returns `returncode=0`. Assert return `0`,
  `dest` has the body, and the argv the fake saw is a **list** with `{in}`/`{out}` replaced by real
  paths (proves no shell interpolation).
- `test_prepare_converter_missing_on_path` — `PREPARE_CMD` set, `shutil.which` → `None` → return `1`,
  message mentions the converter name and "PATH".
- `test_prepare_refuses_existing_dest_without_force` — pre-create `dest`; return `1` mentioning
  `--force`; `--force=True` overwrites and returns `0`.
- `test_prepare_passthrough_text_file` — a `.txt`/`.html` input copied to `<stem>.md` without
  importing pypdf or shelling out; return `0`, content preserved.

**Invariants & risks:**
- **No shell in the request path (2 & 3)** — all of F6 lives in `llamacag.py`, never in `cag-api`.
  Even in the CLI the converter runs via `subprocess.run(argv_list)` with `{in}`/`{out}` substituted as
  whole argv elements — no `shell=True`, no string-built command line, so a malicious filename cannot
  inject arguments.
- **CLI must not import `api.app`** — `llamacag.py` is stdlib-only and runs from a bare clone without
  `pip install -e ./api`. Importing `app.extract` would couple it to FastAPI/pydantic. F6 re-implements
  the ~4-line text-layer probe with a **lazy** pypdf import guarded by `ImportError`. Failure mode
  avoided: a bare-clone user gets a clear next step, not a traceback.
- **Faithful-text contract** — F6 only *produces* text; ingestion still independently extracts/validates
  whatever `.md` lands in the watch folder. A garbage-emitting converter is caught by the README-mandated
  human review; F6 deliberately does not auto-ingest, so nothing wrong reaches the KV cache unreviewed.
  The `SYSTEM_TEMPLATE`/KV invariants (1) and two-lock discipline (4) are untouched (no engine code changes).
- **Additive-only (5)** — no existing subcommand/endpoint/config/response changes; `prepare` and the two
  env knobs are new. `api/app/config.py` is intentionally untouched, so the three-places rule (6) does
  not trigger.
- **Test-collection risk** — `pytest api` uses `testpaths=["tests"]` under `api/` (pyproject.toml:37); a
  repo-root test would be collected by neither `api` nor `mcp`. Placing `test_prepare.py` under
  `api/tests/` with a `sys.path` shim keeps it in an existing CI job with **no CI-workflow change and no
  new dependency** (7). `ruff check api` lints it.
- **Partial-output risk** — writing to `dest.md.partial` and `replace()`-ing only on success means a
  failed conversion leaves the watch folder clean (no truncated `.md` for ingestion to trust).

**Acceptance (done when):**
- [ ] `python llamacag.py prepare <text-layer.pdf>` writes a `.md` to the watch folder (or `--out`)
  with no converter configured and without shelling out.
- [ ] `python llamacag.py prepare <scanned.pdf>` with no `PREPARE_CMD` prints a guided error naming
  marker / docling / local-vision options and the `PREPARE_CMD` env var, and exits `1`.
- [ ] With `PREPARE_CMD` set, a scanned/image PDF is converted via `subprocess.run([...])` (list argv,
  no shell) and the resulting `.md` lands in the destination; a missing/failed converter → clear message,
  exit `1`.
- [ ] `.env.example` documents `PREPARE_CMD` and `PREPARE_OUT_FOLDER` with local-vs-cloud examples; the
  README "Preparing documents" section shows the command, the privacy trade-off, and the review-before-trust step.
- [ ] `api/tests/test_prepare.py` covers text-layer-written and missing-converter cases; `pytest api -q`
  and `pytest mcp -q` green; `ruff check --no-cache api` clean.
- [ ] No change to any `cag-api` endpoint, engine method, `api/app/config.py`, or workflow JSON; `docker
  compose config -q` still passes.

---

### Phase 1 — F4 — per-canon reliability battery (calibration)

**Goal & user value:** Give an operator a mechanical, canon-specific reliability number instead of a
hand-wave. `POST /documents/{id}/calibrate` runs a caller-supplied known-answer Q/A battery against a
document at `temperature=0`, scores each answer against its expected value, and returns
`{n, correct, accuracy, misses}`. This measures *this* canon under *this* model — directly quantifying
the long-context "absent-miss" (lost-in-the-middle) risk so the user knows the expected escalation rate
before trusting the oracle on that document, and can pick a safe (model × canon-size) operating point.

**Effort & dependencies:** M. Depends on **F1** for `api/app/grounding.py` (F4 reuses F1's `grounding()`
for the fuzzy tiebreak; it does **not** need F1's `/verify` endpoint). **Shared surfaces:** `CagEngine.query()`
(called per item, unmodified); `DOCUMENT_COLUMNS`/`GET /documents` and the `documents` table if the optional
`reliability` column ships (shares the migration precedent with F5). **Soft edge:** if F1 is not yet merged
when F4 is built, ship the scorer against a **local private helper** `_score_answer()` (normalized
containment + `difflib.SequenceMatcher` inline) and swap the fuzzy branch to `grounding()` when F1 lands
(same threshold semantics) — this keeps F4 independently buildable.

**Files touched:**
- `api/app/cag.py` **(modified)** — add `calibrate()` and module-level `_normalize()`/`_score_answer()`.
- `api/app/main.py` **(modified)** — add `CalibrateItem`/`CalibrateRequest` models and
  `POST /documents/{document_id}/calibrate`; extend `index()`.
- `api/app/config.py` **(modified)** — add `calibrate_max_items: int = 100` and
  `calibrate_match_threshold: float = 0.85`.
- `api/app/grounding.py` **(dependency, from F1)** — consumed read-only; not created here.
- `api/app/db.py` **(modified, optional column)** — add `reliability` to `DOCUMENT_COLUMNS`; add
  `set_reliability(document_id, accuracy)`.
- `database/schema.sql` **(modified, optional column)** — add nullable `reliability` for fresh volumes.
- `database/migrations/002_reliability.sql` **(new, optional column)** — `ALTER TABLE` for existing
  deployments. **(002, not 001 — F5's optional follow-up claims `001_cache_source.sql`; see the
  migration-ordering note in §7.)**
- `api/tests/test_cag.py`, `api/tests/test_api.py`, `api/tests/conftest.py` **(modified)**.
- `n8n/workflows/calibration-workflow.json` **(new, optional)** — webhook wrapper.
- `README.md` **(modified)** — "Know your canon's reliability" subsection.

**Interface / API changes:**
- **New endpoint** `POST /documents/{document_id}/calibrate`
  - Request: `{"qa": [{"question": str, "expected": str}, ...], "strict": bool = false, "max_tokens": int | null = null}`.
    `qa` non-empty, length ≤ `settings.calibrate_max_items`; each `question`/`expected` `min_length=1`.
    `strict` switches exact-match-only scoring. Over-cap or empty → **422**; unknown `document_id` → **404**
    (reuses the existing `UnknownDocumentError` handler).
  - Response: `{"document": {"id","file_name","n_tokens"}, "n": int, "correct": int, "accuracy": float,
    "strict": bool, "misses": [{"question","expected","got"}]}`. `accuracy = round(correct/n, 4)`; `misses`
    lists only failed items.
- **`GET /documents`** — *additive*: rows gain a nullable `reliability` field (last computed accuracy,
  `null` until calibrated). Existing consumers ignore unknown keys.
- **`GET /`** index — append the calibrate route string (additive).
- **Config knobs** — `calibrate_max_items` (battery cap) and `calibrate_match_threshold` (containment/fuzzy
  pass line for non-strict scoring, distinct from F1's `quote_match_threshold` so calibration strictness
  tunes independently). Behavioral-only → three-places rule N/A; add commented lines to `.env.example` for
  discoverability only.
- **Workflow (optional)** `calibration-workflow.json`: `webhook (POST cag/calibrate, v2)` → `httpRequest 4.2`
  (POST `http://cag-api:8000/documents/{{ $json.body.document_id }}/calibrate`, body `{qa, strict}`,
  `onError:"continueErrorOutput"`, `timeout:3600000`) → `respondToWebhook 1.1` (success) with an error-branch
  `respondToWebhook` (responseCode 502). No `splitOut`/`aggregate` — the engine returns the whole battery
  result in one response.

**Implementation steps:**
1. **`config.py`**:
   ```python
   # Calibration battery: max Q/A pairs accepted by POST /documents/{id}/calibrate.
   calibrate_max_items: int = 100
   # Pass line for non-strict answer scoring (normalized containment always counts;
   # below it, difflib ratio must clear this). Distinct from quote_match_threshold.
   calibrate_match_threshold: float = 0.85
   ```
2. **`cag.py` scorer** — module-level, pure, stdlib-only:
   ```python
   def _normalize(text: str) -> str:
       return " ".join(text.split()).casefold()

   def _score_answer(got: str, expected: str, *, strict: bool, threshold: float) -> bool:
       exp, ans = _normalize(expected), _normalize(got)
       if not exp:
           return False               # empty expected is a spec error, never a pass
       if strict:
           return ans == exp
       if exp in ans:                 # normalized containment: the primary signal
           return True
       from difflib import SequenceMatcher
       return SequenceMatcher(None, exp, ans).ratio() >= threshold
   ```
   When F1 has landed, the fuzzy branch instead calls `grounding(expected, got)["match_ratio"] >= threshold`.
   Containment-first is deliberate: a correct short answer ("12 A") embedded in a verbose reply must score
   correct, which a whole-string ratio would miss.
3. **`cag.py` `calibrate()`**:
   ```python
   def calibrate(self, document_id: int, qa: list[dict], *, strict: bool = False,
                 max_tokens: int | None = None) -> dict:
       doc = self._db.get_document(document_id, with_content=True)
       if doc is None:
           raise UnknownDocumentError(f"No document with id {document_id}")
       threshold = self._settings.calibrate_match_threshold
       correct, misses = 0, []
       for item in qa:
           question, expected = item["question"], item["expected"]
           result = self.query(question, document_id=document_id, temperature=0.0, max_tokens=max_tokens)
           got = result["answer"]
           if _score_answer(got, expected, strict=strict, threshold=threshold):
               correct += 1
           else:
               misses.append({"question": question, "expected": expected, "got": got})
       n = len(qa)
       accuracy = round(correct / n, 4) if n else 0.0
       if hasattr(self._db, "set_reliability"):   # optional column path
           self._db.set_reliability(document_id, accuracy)
       return {"document": {"id": doc["id"], "file_name": doc["file_name"], "n_tokens": doc["n_tokens"]},
               "n": n, "correct": correct, "accuracy": accuracy, "strict": strict, "misses": misses}
   ```
   Key design points: **reuses `query()` verbatim** so the battery runs the exact same message
   construction, the same hot slot, the same `SYSTEM_TEMPLATE` — this satisfies invariant 1 automatically
   (`calibrate()` builds *no* prompt of its own). The 404 check happens **before** any query (fail fast).
   `temperature=0.0` is forced (determinism is the whole point). Each `query()` writes to `query_log`, so a
   calibration run is visible in `/stats` (F5) and the receipt. Serialization: each `query()` takes `_lock`
   for its own generation and releases between items, so `health()` stays responsive (invariant 4 respected —
   `calibrate()` touches no slot map).
4. **`main.py`**:
   ```python
   class CalibrateItem(BaseModel):
       question: str = Field(min_length=1)
       expected: str = Field(min_length=1)

   class CalibrateRequest(BaseModel):
       qa: list[CalibrateItem] = Field(min_length=1)  # cap enforced in route
       strict: bool = False
       max_tokens: int | None = Field(default=None, ge=1, le=8192)

   @app.post("/documents/{document_id}/calibrate")
   def calibrate(request: Request, document_id: int, body: CalibrateRequest):
       engine = _engine(request)
       cap = engine.settings.calibrate_max_items
       if len(body.qa) > cap:
           return JSONResponse(status_code=422, content={
               "detail": f"Calibration battery has {len(body.qa)} items but the cap is {cap}. "
                         f"Raise CALIBRATE_MAX_ITEMS or split the battery."})
       return engine.calibrate(document_id, [item.model_dump() for item in body.qa],
                               strict=body.strict, max_tokens=body.max_tokens)
   ```
   The cap is enforced in the route (not only Pydantic) so the error message can name the env knob,
   mirroring the upload-cap 413 pattern (main.py:141). `UnknownDocumentError` from the engine is translated
   to 404 by the existing handler.
5. **Optional `reliability` column** (ship as a clearly-separable second commit):
   - `db.py`: append `reliability` to `DOCUMENT_COLUMNS` (so every `SELECT` and `GET /documents` carries it);
     add:
     ```python
     def set_reliability(self, document_id: int, accuracy: float) -> bool:
         return self._one(
             "UPDATE documents SET reliability = %s WHERE id = %s RETURNING id",
             (accuracy, document_id),
         ) is not None
     ```
   - `schema.sql`: add `reliability REAL,` to the `documents` table (nullable; fresh volumes only).
   - `database/migrations/002_reliability.sql` **(new)**: `ALTER TABLE documents ADD COLUMN IF NOT EXISTS
     reliability REAL;` with a header explaining schema.sql runs only on a fresh volume and the one-time
     `docker compose exec -T db psql … < …` apply. `IF NOT EXISTS` makes it idempotent.
   - README "Updating & maintenance": a one-line pointer.
6. **README** — "Know your canon's reliability" subsection: the `curl`, the caller-supplied-ground-truth
   caveat (measures *this canon under this model*, not the model in general), and the escalation-rate framing
   (accuracy ≈ 1 − expected `absent`-miss rate for that battery). Note `reliability` on `GET /documents`.

**Tests to add:**
- **`conftest.py`** (extend fakes):
  - `FakeLlama`: add `self.scripted: dict[str, str] = {}` and, in `chat()`, derive from the *last user
    message* — `q = messages[-1]["content"]; return {... "content": self.scripted.get(q, self.answer) ...}`.
    Existing tests unaffected (empty dict → `self.answer`). Composes cleanly with F1's `answer_json`
    (F1/F4 tests never set both; `scripted` keys off the user turn while `answer_json`/`answer` is the fallback).
  - `FakeDatabase`: add `"reliability": None` to the `insert_document` dict (carried by `_public`) and
    `set_reliability(document_id, accuracy)` returning the row-existed bool.
- **`test_cag.py`**:
  - `test_calibrate_scores_and_lists_misses` — `scripted = {"q1":"Fredville","q2":"wrong","q3":"42"}`;
    `calibrate(1, [{"question":"q1","expected":"Fredville"}, {"question":"q2","expected":"Metropolis"},
    {"question":"q3","expected":"42"}])`; assert `n==3`, `correct==2`, `accuracy==round(2/3,4)`,
    `misses==[{"question":"q2","expected":"Metropolis","got":"wrong"}]`.
  - `test_calibrate_containment_counts_correct` — expected `"12 A"`, scripted `"The peak current limit is
    12 A."` → correct, `misses==[]`.
  - `test_calibrate_strict_requires_exact` — same containment case with `strict=True` → a miss (`correct==0`).
  - `test_calibrate_fuzzy_tiebreak_passes_near_match` — expected `"thermal shutdown at 150C"`, scripted
    `"thermal shutdown at 150 C"` → non-strict pass via ratio ≥ threshold (guards the fuzzy branch / F1-swap).
  - `test_calibrate_unknown_document_raises` — `calibrate(999, [...])` raises `UnknownDocumentError`; assert
    `fake_llama.called("chat") == []` (fails before any query).
  - `test_calibrate_runs_through_query_path` — after calibrate, `len(fake_db.queries) == n` and every logged
    item has `success is True`.
  - `test_calibrate_sets_reliability_column` — `fake_db.documents[1]["reliability"] == round(2/3, 4)`.
- **`test_api.py`**:
  - `test_calibrate_endpoint_happy_path` — ingest via `/documents/text`; `scripted` set; `POST
    /documents/1/calibrate` (2-item battery) → 200, body `accuracy`/`misses`/`n`, `document.id == 1`.
  - `test_calibrate_unknown_document_is_404`.
  - `test_calibrate_over_cap_is_422` — battery of `calibrate_max_items + 1` (tiny-cap client fixture with
    `calibrate_max_items=2`) → 422, `detail` contains `CALIBRATE_MAX_ITEMS`.
  - `test_calibrate_empty_battery_is_422` — `{qa: []}` → 422 (Pydantic `min_length=1`).
  - `test_calibrate_reliability_surfaces_on_list` — after calibrate, `GET /documents` row has `reliability`
    equal to the returned `accuracy`.

**Invariants & risks:**
- **Invariant 1** — respected structurally: `calibrate()` constructs no messages; it only calls `query()`,
  so the cached prefix is untouched and KV reuse holds across the whole battery. This is the single most
  important reason to route through `query()` rather than a bespoke completion loop.
- **Invariant 3** — inherited: a cold cache self-heals on the *first* item; the rest hit the now-hot slot.
- **Invariant 4** — `calibrate()` never takes `_lock`/`_slots_guard`; it composes over `query()` (lock
  released between items), so a long battery does not starve `health()`.
- **Invariant 5** — new route, additive `reliability` field, additive index entry; nothing changed/removed.
  `reliability` is nullable so consumers that ignore unknown keys are unaffected.
- **Invariant 2** — `set_reliability` uses a bound parameter; no process spawning.
- **Failure modes & mitigations:** runaway battery → capped at `calibrate_max_items` (422 naming the knob) +
  per-item `max_tokens`; a mid-battery `LlamaError` propagates to the existing `LlamaError`→502 handler
  (chosen over swallowing so a score is never computed over a degraded server — partial-battery resilience
  would be an additive `on_error` flag, not a default); false "correct" from a substring coincidence
  (expected `"12"` in `"120"`) → documented limitation, reduced by `strict=True` + word-normalization, and
  acceptable because calibration is an operator estimate, not a gate; caller-supplied ground truth → the
  response echoes `strict` and `misses` and the README states plainly this measures *this canon under this
  model*; F1 not yet merged → the inline `_score_answer` fallback keeps F4 green (swap to `grounding()` is a
  one-line change, covered by the fuzzy-tiebreak test); migration drift → the `reliability` column ships in
  **both** `schema.sql` and `migrations/002_reliability.sql` (idempotent `IF NOT EXISTS`), and
  `set_reliability` is behind `hasattr`, so the core feature ships independently of the column.

**Acceptance (done when):**
- [ ] `POST /documents/{id}/calibrate` returns `{n, correct, accuracy, strict, misses}`; a 2-of-3 fixture
  yields `accuracy == round(2/3, 4)` with the one miss listed.
- [ ] Unknown `document_id` → 404; empty battery → 422; over-cap battery → 422 naming `CALIBRATE_MAX_ITEMS`.
- [ ] Scoring: normalized containment passes an embedded short answer; `strict=True` requires exact; fuzzy
  tiebreak passes a near-match at/above threshold.
- [ ] The battery runs through the real `query()` path (each item in `query_log`; `temperature=0`), leaving
  `SYSTEM_TEMPLATE` and the KV cache untouched.
- [ ] (Optional column) `reliability` is nullable in `schema.sql`, ships as
  `database/migrations/002_reliability.sql` (idempotent), is set after calibration, and surfaces on
  `GET /documents`.
- [ ] (Optional) `calibration-workflow.json` imports and validates under the CI check.
- [ ] `ruff check --no-cache api` clean; `pytest api` and `pytest mcp` green; `docker compose config -q`
  unaffected; README "Know your canon's reliability" subsection added.

---

### Phase 2 — F2 — answer-gating pattern + fail-safe gate

**Goal & user value:** Ship a documented pattern plus an optional n8n workflow that gates a support
bot's draft answer against the pinned canon: ask the oracle the *original question* fresh (grounded,
`temperature 0`) to get a reference answer G, then have `/verify` (F1) confirm the draft is fully
supported, and auto-pass **only** on `verdict=="supported"` with a grounded quote — routing everything
else to human review. This gives non-technical operators a one-webhook "is this answer safe to send?"
check that catches conclusion/reasoning errors a fact-by-fact decomposition would miss, and it
establishes the fail-safe rule (default to escalation) as reusable infrastructure.

**Effort & dependencies:** **S**. Hard-depends on **F1** (`POST /verify` returning `quote_grounded`); no
core code beyond F1. Shares the README oracle section with F1/F3. Shares the n8n single-item error-branch
pattern with `query-workflow.json` (error → `respondToWebhook` responseCode 502, verified at
query-workflow.json:60–73) and the `/verify` contract with `claim-verification-workflow.json`. If F3 has
landed, the gate additionally surfaces `conditions` (additive Set field) — noted but not required.

**Files touched:**
- `n8n/workflows/answer-gate-workflow.json` **(new)**
- `README.md` **(modified — one new subsection under "The grounding oracle")**
- `docs/ROADMAP.md` **(modified — F2 status row)**

**Interface / API changes:** **No API changes** — pure HTTP orchestration over existing endpoints.
- **Consumes** (unchanged, from F1): `POST /query {question, temperature, document_id?}` →
  `{answer, document, timings.cache_source, …}`; `POST /verify {claim, document_id?}` →
  `{claim, verdict, quote, quote_grounded, match_ratio, document, timings}`.
- **New webhook:** `POST /webhook/cag/answer-gate {question, draft, document_id?}` →
  `{pass, verdict, quote, grounded_answer, reason}` (200), or `{pass:false, verdict:null, quote:null,
  grounded_answer:null, reason:"…error…"}` (502) on the error branch.
- **Workflow nodes** (all whitelisted, matching shipped typeVersions): `webhook` (2), `httpRequest` (4.2)
  ×2, `set` (3.4) ×2, `respondToWebhook` (1.1) ×2, `stickyNote` (1). `settings.executionOrder="v1"`,
  `pinData:{}`, `active:false`, unique `webhookId`.

**Implementation steps:**
1. **Author `answer-gate-workflow.json`** as a linear two-hop chain (not a splitOut fan-out — one draft
   in, one verdict out), mirroring `query-workflow.json`'s single-item error-branch shape but with two
   sequential HTTP calls. Eight nodes:
   - **Sticky note "How to use"** (`stickyNote` v1) with a `## CAG Answer Gate` heading, the curl below,
     the fail-safe rule stated once, and the "No credentials needed — cag-api does all the work." line,
     matching the other notes' structure:
     ```
     curl -X POST http://localhost:5678/webhook/cag/answer-gate \
       -H "Content-Type: application/json" \
       -d '{ "document_id": 7,
             "question": "Does the warranty cover water damage?",
             "draft": "Yes — the warranty fully covers water damage." }'
     ```
   - **"Answer Gate Webhook"** (`webhook` v2): `httpMethod:"POST"`, `path:"cag/answer-gate"`,
     `responseMode:"responseNode"`, `options:{}`, a fresh unique `webhookId` (UUID v4 distinct from the
     five existing — e.g. query's `3f7f4b7e-…`, verify's `7c2dae3f-…`).
   - **"Grounded Reference Answer"** (`httpRequest` v4.2): `POST http://cag-api:8000/query`,
     `sendBody:true`, `specifyBody:"json"`, `options:{timeout:3600000}`, `onError:"continueErrorOutput"`.
     Body:
     ```
     ={{ JSON.stringify({ question: $json.body.question, temperature: 0, document_id: $json.body.document_id }) }}
     ```
     Temperature 0 is load-bearing: the reference answer G must be reproducible so the gate is
     deterministic for a given (question, document).
   - **"Verify Draft Support"** (`httpRequest` v4.2): `POST http://cag-api:8000/verify`, same
     body/timeout/onError. It runs on the success output of the reference node, so it references the
     original webhook body via `$('Answer Gate Webhook').item.json.body`. Body:
     ```
     ={{ JSON.stringify({ claim: 'This answer is fully supported by the document: ' + $('Answer Gate Webhook').item.json.body.draft, document_id: $('Answer Gate Webhook').item.json.body.document_id }) }}
     ```
     (F1's `/verify` builds its own prompt and schema server-side, so the workflow only supplies the
     claim text.)
   - **"Apply Gate"** (`set` v3.4) on the success output of "Verify Draft Support" — assignments (all
     strings except `pass`, boolean):
     - `pass` (boolean): `={{ $json.verdict === 'supported' && $json.quote_grounded === true }}` — the
       fail-safe rule: strict equality on both, so any non-`supported` verdict, a null/missing
       `quote_grounded`, or a fabricated-quote (`quote_grounded:false`) yields `false`.
     - `verdict`: `={{ $json.verdict }}`; `quote`: `={{ $json.quote }}`.
     - `grounded_answer`: `={{ $('Grounded Reference Answer').item.json.answer }}` — G, so a reviewer sees
       what the source actually supports next to the draft.
     - `reason`: `={{ $json.verdict === 'supported' && $json.quote_grounded === true ? 'Draft is
       grounded-supported by the document.' : 'Escalated: verdict=' + $json.verdict + ',
       quote_grounded=' + $json.quote_grounded + '. Only supported+grounded auto-passes.' }}`.
     - (If F3 landed: additionally pass through `conditions`: `={{ $json.conditions }}` and fold guidance
       into `reason` — a present condition is a review *signal*, decided in docs, **not** folded into
       `pass` as an auto-fail.)
   - **"Respond With Gate"** (`respondToWebhook` v1.1) on "Apply Gate": `respondWith:"json"`,
     `responseBody:"={{ $json }}"`, `options:{}` (mirrors query-workflow's success responder).
   - **"Mark Failure"** (`set` v3.4): reachable from the **error output of _both_ HTTP nodes** so a failure
     in either hop produces the same shape. Assignments: `pass`=`={{ false }}` (boolean), `verdict`/`quote`/
     `grounded_answer`=`={{ null }}`, `reason`=`={{ $json.error?.message ?? $json.error ?? 'CAG API request
     failed' }}`. Fail-closed by construction.
   - **"Respond With Error"** (`respondToWebhook` v1.1) on "Mark Failure": `respondWith:"json"`,
     `responseBody:"={{ JSON.stringify($json) }}"`, `options:{responseCode:502}` (matches query-workflow's
     error responder status).
2. **Wire `connections`** exactly (all `type:"main"`, `index:0`) so the CI connection-integrity check
   passes: `Answer Gate Webhook` → `[[Grounded Reference Answer]]`; `Grounded Reference Answer` →
   `[[Verify Draft Support], [Mark Failure]]` (output 0 success, output 1 error); `Verify Draft Support` →
   `[[Apply Gate], [Mark Failure]]`; `Apply Gate` → `[[Respond With Gate]]`; `Mark Failure` →
   `[[Respond With Error]]`. Two nodes fanning into `Mark Failure` is valid (n8n merges error inputs; the
   CI validator only checks name existence).
3. **Position nodes** left-to-right (sticky `[-320,-220]`; webhook `[200,0]`; reference `[460,0]`; verify
   `[720,0]`; gate `[980,-100]`; success responder `[1240,-100]`; failure Set `[980,120]`; error responder
   `[1240,120]`).
4. **README subsection** — insert **after line 141** (the fail-safe-gate paragraph) and **before `### It
   composes with LLM wikis`**, as a peer `###` heading **"Gating a support bot's answers"**: (a) the *why*
   — for a support bot the question already exists, so **answer-compare** (regenerate the grounded answer,
   verify the draft against the source) is the right architecture, whereas **decompose-and-verify** (split
   the draft into atomic claims and check each) verifies isolated facts but can pass a draft whose facts are
   individually true yet whose *conclusion* is wrong; answer-compare needs **no decomposition step** and
   checks the thing that ships — the conclusion — in **one grounded generation**; (b) the fail-safe rule
   stated once (auto-pass only on `supported` + grounded quote; all else → review), linking back to the
   fail-safe-gate paragraph; (c) the curl with an annotated `# → {"pass": false, "verdict": "contradicted",
   …}` line; (d) a one-liner that the bundled `answer-gate` workflow implements exactly this. Keep the
   em-dash/bold house style.

**Tests to add:** No Python/conftest tests — this feature adds **zero core code**. Verification surface:
- **CI "Validate workflows" job** (existing, auto-covers the new file): asserts the file parses, uses no
  deprecated nodes, and every `connections` source/target resolves to a node `name`. Adding the file makes
  CI validate **six** workflows; confirm the job prints `OK n8n/workflows/answer-gate-workflow.json (8 nodes)`.
- **Manual import check** (documented in the PR): import into n8n 2.x, POST a draft that overstates the
  document → `pass:false`; POST a draft the grounded answer supports → `pass:true`.
- **Design guard:** if any scoring logic beyond the Set expression proves necessary, it moves into a
  `/verify`-style endpoint (with `FakeLlama` tests), never an n8n Code node (banned by convention + CI).

**Invariants & risks:**
- **(1)** respected trivially — the gate never touches the system prefix; the draft rides in `/verify`'s
  *claim* (a server-side user turn) and `temperature 0` is a *sampling* setting.
- **(5)** respected — no endpoint added or changed; the workflow is a pure client. The webhook's own output
  object is new surface (no existing consumer).
- **(2)** respected — no core request-path code; all logic is n8n expressions.
- **n8n whitelist & versions** — only webhook / httpRequest 4.2 / set 3.4 / respondToWebhook 1.1 /
  stickyNote 1.
- **Failure modes & how the design avoids them:** model returns non-JSON despite the schema → F1's `/verify`
  returns `verdict:"error"` (not 500), so "Apply Gate" sees `verdict!=='supported'` → `pass:false`;
  `quote_grounded` null/absent (e.g. `absent` verdict, or F1 not yet deployed) → strict `=== true` yields
  `false`, so the draft escalates rather than silently passing; either HTTP hop errors (llama down → 502,
  unknown `document_id` → 404) → `onError:"continueErrorOutput"` routes to "Mark Failure" → `pass:false`,
  502 response (never fails *open*); `document_id` omitted → both `/query` and `/verify` default to the
  latest cached doc, and both read `document_id` from the *identical webhook body* (not from each other's
  response), so they target the same document — recommend passing `document_id` explicitly to remove the
  concurrent-ingest ambiguity (edge note in the README); draft contains quotes/newlines/braces →
  `JSON.stringify` over the whole body escapes them, and the claim is a plain string concatenation with no
  manual quoting, so no injection into the JSON body.

**Acceptance (done when):**
- [ ] `answer-gate-workflow.json` exists, is valid JSON, `active:false`, `executionOrder:"v1"`,
  `pinData:{}`, with a unique `webhookId` and exactly the eight nodes above at the specified typeVersions.
- [ ] CI "Validate workflows" passes and prints `OK …/answer-gate-workflow.json (8 nodes)`.
- [ ] Imports cleanly into n8n 2.x; a draft that overstates the document → `pass:false` with a
  `contradicted`/`absent`/non-grounded verdict; a draft the grounded answer supports → `pass:true` with
  `quote_grounded:true`.
- [ ] Any error in either HTTP hop → `pass:false` + HTTP 502 (fail-closed).
- [ ] README gains "Gating a support bot's answers" under the oracle (why answer-compare beats
  decompose-and-verify; fail-safe rule once; the curl), placed after line 141, before "It composes with LLM
  wikis".
- [ ] `ROADMAP.md` F2 status row updated to reflect shipped.

---

### Phase 2 — F9 — zero-install web UI (served at `/ui`)

**Goal & user value:** Give the non-technical user a face that needs nothing installed: run
`python llamacag.py start`, open `http://localhost:8000/ui`, then drag in a document, chat with it
(cache-source chip + token receipt), verify a list of claims, and see which documents are Hot/Disk/Cold.
It complements the native LlamaCag UI (power-user control room) and n8n (automation) as the casual
daily-driver front door, and it is where the oracle finally gets a GUI and residency becomes visible —
all as a pure same-origin client of endpoints that already exist.

**Effort & dependencies:** M (effort, not risk — the ROADMAP's 11/11 vertical-slice feasibility note
confirms every tab's data path works against the real `create_app()`). No dependency on other F# to ship
(uses only `/documents`, `/documents/text`, `/query`, `/health`, `/maintenance`, `DELETE`). Two optional
progressive enhancements: the **Verify** tab prefers `POST /verify` when **F1** is present (adds the
`quote_grounded` column) and falls back to `/query`+`json_schema` before then; the **Stats** tab lights up
a cumulative-savings line when **F5**'s `GET /stats` is present. Shared surface with **F10** (the empty-state
"Try a sample" affordance lives in this SPA). Shared files: `api/app/main.py` (one mount block) and
`api/app/config.py` (optional `webui_enabled` knob).

**Files touched:**
- `api/app/webui/index.html` **(new)** — self-contained vanilla-JS SPA, inline `<style>`/`<script>`, zero
  external refs.
- `api/app/main.py` **(modified)** — mount `StaticFiles`.
- `api/app/config.py` **(modified)** — optional `webui_enabled: bool = True`.
- `api/pyproject.toml` **(modified)** — ship the non-`.py` asset in the wheel/editable install (package-data).
- `api/tests/test_api.py` **(modified)** — `GET /ui/` smoke test (+ a mount-off test if the flag is added).
- `.env.example`, `docker-compose.yml`, `docker-compose.gpu.yml`, `docker-compose.vulkan.yml` **(modified)** —
  only if the `webui_enabled` knob is added (three-places rule).
- `README.md` **(modified)** — quick-start line + screenshot + security-boundary paragraph.

**Interface / API changes:**
- **No endpoint changes.** Additive only: a new static mount at path prefix `/ui` (serves `index.html` at
  `/ui/`). Everything the SPA needs is already returned:
  - `GET /documents` → `{documents: [{id, slug, file_name, n_tokens, cache_file, status, error, created_at,
    cached_at, last_used_at, use_count}, …]}` (from `DOCUMENT_COLUMNS`, db.py:9; **no `content`**).
  - `GET /health` → `{status, hot_documents: {"<slot>": <document_id>}, slots, llama_server, database}` —
    `hot_documents` (cag.py:575) is the slot→doc-id map that drives Hot/Disk/Cold.
  - `POST /query` → `{answer, document{id,file_name,n_tokens}, duration_ms, timings{prompt_tokens_evaluated,
    prompt_tokens_from_cache, answer_tokens, cache_source, history_trimmed?}}`. `cache_source ∈
    {"memory","disk","recomputed"}`.
  - `POST /documents` (multipart `file`), `POST /documents/text` `{text, file_name?}`, `DELETE
    /documents/{id}` → `{deleted: id}`.
- **Config knob (optional):** `WEBUI_ENABLED` (bool, default `true`). If added, follow the three-places
  rule: `api/app/config.py` (`webui_enabled: bool = True`), the `${WEBUI_ENABLED:-true}` fallback in all
  three `docker-compose*.yml` `cag-api` environment blocks, and a commented `#WEBUI_ENABLED=true` in
  `.env.example`.
- **Workflow nodes:** none.

**Implementation steps:**
1. **Create `api/app/webui/index.html`** — one file, no build step, no CDN/font/image refs (CSP-clean and
   offline-safe, matching the self-contained-SVG convention). `<style>` defines the palette as CSS variables
   on `:root` (`--bg:#0F172A; --panel:#1E293B; --border:#334155; --text:#E2E8F0; --muted:#94A3B8;
   --amber:#F59E0B; --cyan:#22D3EE; --green:#34D399; --red:#F87171;`), system font stack only, a top tab-bar,
   `.panel`, chips (`.chip.memory/.disk/.cold/.hot`), a CSS spinner (no GIF). `<body>` has a header with tab
   buttons **Chat · Library · Verify · Stats**, a `<main id="view">` swapped by JS, and an empty-state block
   (F10 hooks here). `<script>` is a vanilla IIFE: a tiny `api(path, opts)` wrapper doing
   `fetch(path, {headers:{'Content-Type':'application/json'}, ...})` against **relative** URLs (same origin
   ⇒ no CORS, no base URL to configure), a central error toast surfacing the JSON `detail` field (mirrors
   how the MCP `_detail` reads it, client.py:95), a `state` object `{docs:[], hot:{}, activeDocId:null,
   history:[]}`, and a `render()` dispatch on the active tab.
2. **Mount in `api/app/main.py`.** Inside `create_app` after routes are registered (a sub-path mount does not
   shadow the JSON routes), just before `return app`:
   ```python
   from pathlib import Path
   from fastapi.staticfiles import StaticFiles
   ...
   _webui_dir = Path(__file__).parent / "webui"
   _mount_webui = _webui_dir.is_dir() and (
       engine.settings.webui_enabled if engine is not None else Settings().webui_enabled
   )
   if _mount_webui:
       app.mount("/ui", StaticFiles(directory=_webui_dir, html=True), name="webui")
   ```
   The `.is_dir()` guard keeps the app importable if the asset is absent (a partial checkout skips the mount,
   never errors). `html=True` serves `index.html` at `/ui/`. Reuse the already-imported `Settings`
   (production) or the injected `engine.settings` (test). If the `webui_enabled` knob is **not** wanted, drop
   the flag term and mount whenever `_webui_dir.is_dir()`.
3. **`api/app/config.py`** (only if gating):
   ```python
   # Serve the zero-install web UI at /ui. Loopback-only by design (the stack is
   # unauthenticated); see the security note in docs/ROADMAP.md F9 before binding
   # the API beyond 127.0.0.1.
   webui_enabled: bool = True
   ```
4. **Package the asset** in `api/pyproject.toml`. `packages.find include=["app*"]` (pyproject.toml:27–28)
   collects the Python package but not a bare `.html`; add so `build: ./api` (Docker) and `pip install -e
   ./api` both ship it:
   ```toml
   [tool.setuptools.package-data]
   app = ["webui/*.html"]
   ```
   (Necessary: `webui/` has no `__init__.py` and only static content; without package-data the editable/wheel
   install can omit the file and the `.is_dir()` guard would silently skip the mount.)
5. **Chat tab.** Doc `<select>` from `GET /documents` filtered to `status === "cached"` (so an un-warmed doc
   can't be queried). Input + Send → `POST /query {question, document_id: activeDocId, history}`. Render the
   answer, a `.chip` colored by `timings.cache_source` (memory→cyan, disk→amber, recomputed→red) labeled
   "from memory / from disk / recomputed", and a receipt line
   `evaluated {prompt_tokens_evaluated} · from cache {prompt_tokens_from_cache} · answer {answer_tokens} ·
   {duration_ms} ms`. Push `{role:"user",content:q}` and `{role:"assistant",content:answer}` into
   `state.history` and pass it on the next call (multi-turn stays cheap via KV reuse). Show
   `timings.history_trimmed` if present.
6. **Library tab.** `GET /documents` as a table: name, tokens, status, and a **residency** column. Compute
   residency by inverting `GET /health` `hot_documents` (`{slot:docId}` → a Set of hot ids): `hot` if the id
   is in that set (green), else `disk` if `cache_file` is non-null (amber), else `cold` (muted). A file input
   **and** a drag-drop zone both call `POST /documents` as `FormData` with the `file` field. During upload
   show "uploading… warming…", then **poll** `GET /documents` every ~1.5 s until the new row's `status` flips
   from `pending` to `cached`/`failed` (warming a large doc can take minutes — the poll must tolerate that;
   cap at a generous timeout and surface `error` on `failed`). Delete → `DELETE /documents/{id}` then refresh.
   Refresh `/health` alongside `/documents` so residency stays current.
7. **Verify tab.** A `<textarea>` (one claim per line). On submit, split non-empty lines and per claim run the
   verification. **Capability probe:** try `POST /verify {claim, document_id}` first; on 404/405 (pre-F1) fall
   back to `POST /query {question:<verify-prompt>, temperature:0, json_schema:<DEFAULT_VERDICT_SCHEMA>}` and
   parse the JSON out of `answer`. Render a verdict table: claim, verdict chip (supported→green,
   contradicted→red, absent→muted, error→amber), the quote, and — when `/verify` was used — a
   **`quote_grounded`** column (true→green ✓, false→red ✗, null→muted "n/a"). Run claims **sequentially**
   (inference is serialized behind `_lock` server-side; parallel `fetch` would just queue and could trip
   timeouts) with a per-row spinner. This is the oracle's first GUI.
8. **Stats tab.** From `GET /health`: overall `status`, `slots`, and the hot-docs map rendered as "slot N →
   doc {id} ({file_name})" by joining against `state.docs`; a llama/database health line. **Progressive:**
   attempt `GET /stats` (F5); if 2xx render the cumulative "compute saved" line (tokens served from cache,
   eval-vs-cached ratio, queries/day, p50/p95, optional cost line); if 404 omit that section silently.
9. **README.** Add "open http://localhost:8000/ui" to the quick start with a one-line description and a
   screenshot; add the **security boundary** paragraph (below).

**Security boundary (must appear in README + a top comment in `index.html`):** The stack is
**unauthenticated by design; loopback is the security boundary.** The web UI is therefore for the **local
host** by default. Reaching it from a phone or another machine means binding the API port beyond
`127.0.0.1`, which exposes an **unauthenticated API on your network** — only do that behind a reverse proxy
with auth, or on a trusted LAN you control. General multi-user access is the **F8** fork, not this feature.

**Tab layout (exact):**
```
┌───────────────────────────────────────────────────────────┐
│  cag-api · web UI          [Chat] [Library] [Verify] [Stats]│  ← header, #1E293B, #334155 border
├───────────────────────────────────────────────────────────┤
│  Chat:    [ doc picker ▾ (cached only) ]                   │
│           ┌─ history transcript ─────────────────────────┐ │
│           │ Q … / A …  + [from disk] chip + receipt line  │ │
│           └──────────────────────────────────────────────┘ │
│           [ question input …………………………… ] [Send]           │
│  Library: [＋ file]  ⇱ drag-drop zone   (uploading… warming…)│
│           name | tokens | status | residency(Hot/Disk/Cold)│
│           …                                        [Delete] │
│  Verify:  ┌ one claim per line ─────────────────────────┐  │
│           └───────────────────────────────────[Verify]──┘  │
│           claim | verdict-chip | quote | quote_grounded ✓/✗ │
│  Stats:   status ● | slots N | slot0→doc3 (manual.md)      │
│           (compute saved …  — shown when /stats exists)     │
└───────────────────────────────────────────────────────────┘
   Empty state (no cached docs): "Nothing ingested yet — [Try a sample]"  (F10)
```

**Tests to add** (`api/tests/test_api.py`, using the conftest `engine` fixture — real
`create_app(engine=engine)` over `FakeLlama`/`FakeDatabase`):
- `test_webui_served_at_ui` — `client.get("/ui/")` → 200; `content-type` starts with `text/html`; body
  contains a distinctive marker (e.g. `id="view"`). The fixture builds the app with the real `webui/` dir
  present (next to `main.py`).
- `test_webui_index_is_self_contained` — read `api/app/webui/index.html`; assert no external references (no
  `http://`/`https://` src/href — only relative or `data:` — and no `src=`/`href=` at a CDN). Cheap guard
  against a future accidental external font/script.
- If the `webui_enabled` flag is added: `test_webui_disabled_returns_404` — build an engine whose
  `settings.webui_enabled = False` (construct `Settings(cache_dir=tmp_path, llama_ctx_size=1000,
  answer_reserve_tokens=100, db_password="test", webui_enabled=False)`, mirroring the `settings` fixture at
  conftest.py:187) → `client.get("/ui/")` returns 404.
- Frontend behavior (tab switching, drag-drop, poll loop) is **effort, not risk** — no unit test now; an
  optional Playwright click-through can come later.

**Invariants & risks:**
- **Invariant 1** — respected trivially: the SPA never sends a system message; it uses `/query`'s existing
  `history` (user/assistant turns) and `json_schema` (sampling) paths only. The Verify fallback puts the
  claim in a **user** turn.
- **Invariant 5** — respected: zero endpoint edits; a new static mount at a fresh path prefix. n8n, MCP
  client, and LlamaCag UI are untouched.
- **Invariant 6** — respected by adding `WEBUI_ENABLED` to config + all three compose files + `.env.example`
  together, or by skipping the knob entirely.
- **Invariant 7** — `StaticFiles` ships with FastAPI/Starlette and `python-multipart` is already a dependency
  (pyproject.toml:14); **no new runtime dep**. The new smoke tests keep `pytest api` green.
- **Failure mode — asset missing from the image** (packages.find ignores non-`.py`): avoided by step 4's
  `package-data`, and the `.is_dir()` guard degrades to "no /ui" instead of a boot crash.
- **Failure mode — StaticFiles shadows JSON routes:** avoided by mounting at `/ui` (a sub-path), not `/`;
  FastAPI matches the explicit routes first and the mount owns only `/ui/*`.
- **Failure mode — long warm makes the upload poll look hung / trips a proxy timeout:** the poll is
  client-side against `GET /documents` (cheap, returns immediately) rather than blocking on the `POST
  /documents` response; the UI shows "warming…" and tolerates minutes.
- **Failure mode — exposing an unauthenticated API on a LAN:** not a code bug but the sharpest operational
  risk; mitigated by the loopback-default (ports bind `127.0.0.1`, docker-compose.yml:64) and the prominent
  security-boundary note pointing multi-user needs at F8.
- **Failure mode — CSP/offline breakage from an external font or script:** avoided by the self-contained rule
  and guarded by `test_webui_index_is_self_contained`.

**Acceptance (done when):**
- [ ] `python llamacag.py start` → `http://localhost:8000/ui` loads with the dark palette, no external
  network requests (DevTools shows only same-origin calls).
- [ ] Chat: pick a cached doc, ask a question, see the answer + correct cache-source chip + token receipt; a
  second turn reuses `history`.
- [ ] Library: drag-drop a `.md`, watch the row go `pending → cached` via polling; residency shows
  Hot/Disk/Cold correctly against `/health`; delete works.
- [ ] Verify: paste 2–3 claims, get a verdict table with colored chips (and the `quote_grounded` column when
  `/verify`/F1 is present, fallback otherwise).
- [ ] Stats: shows status/slots/hot-docs now; the savings line appears only when `/stats`/F5 is present.
- [ ] `GET /ui/` → 200 text/html smoke test passes; `ruff check --no-cache api` clean; `pytest api` and
  `pytest mcp` green.
- [ ] README quick start names the `/ui` URL and states the security boundary; the `index.html` carries the
  same warning as a top comment.

---

### Phase 3 — F10 — sample documents + guided first-run

**Goal & user value:** Kill the empty-state cliff so a first-time user reaches a real "aha — it remembers"
answer in under a minute, before any of their own files are involved. Ship two curated, fact-dense sample
documents and a one-click "Try a sample" path in the web UI that ingests a sample and drops the user
straight into Chat with a suggested question — and, because one sample encodes a **conditional** fact, the
Verify tab has a genuine contradiction to catch.

**Effort & dependencies:** S. Depends on **F9** for the UI affordance (the "Try a sample" button lives in
F9's empty state). The sample files and the ingest test are standalone and can land first. No shared engine
surface — uses the existing `POST /documents/text`.

**Files touched:**
- `samples/acme-widget-manual.md` **(new)** — short, dense fake product manual.
- `samples/refund-policy.md` **(new)** — one-page policy carrying the conditional fact.
- `api/app/webui/index.html` **(modified, from F9)** — empty-state "Try a sample" button + suggested-question
  hand-off.
- `api/tests/test_api.py` **(modified)** — sample-ingest test.
- `README.md` **(modified)** — one line in the "Use it" block naming the samples.

**Interface / API changes:**
- **No new endpoints.** The "Try a sample" button `POST`s the sample's text via existing `POST
  /documents/text {text, file_name}` → `{…, status:"cached", id, …}` (same additive response shape). No
  config knobs, no workflow nodes.

**Implementation steps:**
1. **Write the samples** — small, self-contained, with checkable facts (numbers **and** at least one
   condition) so Verify can catch a contradiction:
   - `samples/acme-widget-manual.md`: a fake "ACME Widget Pro" manual with hard, quotable facts — e.g. *"The
     battery lasts 18 hours on a full charge."*, *"Operating temperature range: 0 °C to 40 °C."*, *"The
     warranty period is 24 months from the date of purchase."*, *"Firmware updates are delivered over Wi-Fi
     only; USB updates are not supported."* These give Chat crisp answers and Verify exact-match
     (`method:"exact"`) grounding targets.
   - `samples/refund-policy.md`: a one-page policy whose central fact is **conditional** — e.g. *"A widget is
     refundable **only if it is defective** and returned within 30 days of delivery. Change-of-mind returns
     are not eligible for a refund."* Plus supporting facts (*"Refunds are processed within 5 business
     days."*, *"Shipping charges are non-refundable."*). The conditional is deliberate: the claim *"Widgets
     are refundable within 30 days"* is **contradicted / conditional**, so Verify surfaces a contradiction
     (and, once F3 lands, a non-empty `conditions`). Keep each file well under the per-slot token limit (a
     few hundred words).
2. **Wire the empty state in `api/app/webui/index.html`.** When `GET /documents` returns no `cached` docs,
   render *"Nothing ingested yet — try a sample to see it remember."* with a **Try a sample** button (offer
   both via a small dropdown: "ACME Widget manual" / "Refund policy"). The samples are embedded as JS string
   constants in `index.html` (the browser can't read the server's `samples/` folder, and inline constants
   preserve the no-external-fetch rule; the `samples/*.md` files are the source of truth for repo/README/tests,
   copied into the constant). On click: `POST /documents/text {file_name:"…", text:<embedded>}`, await
   `{id, status}`; if `status === "cached"` (or after the standard warm poll) set `state.activeDocId = id`,
   switch to **Chat**, and pre-fill the input with a suggested question matched to the sample (manual → *"How
   long does the battery last?"*; policy → *"Can I get a refund if I changed my mind?"* — the latter
   demonstrates the conditional). Surface a hint: *"Now switch to Verify and paste: 'Widgets are refundable
   within 30 days' to see the oracle catch the condition."*
3. **README.** Add one line to the "Use it" block: *"No documents yet? Open `/ui` and click **Try a sample**
   — or `POST` `samples/refund-policy.md` — to see grounded answers and a Verify catch immediately."*

**Tests to add** (`api/tests/test_api.py`, conftest `client` fixture over the fakes):
- `test_sample_ingests_to_cached` — read `samples/refund-policy.md` from disk (resolve via
  `Path(__file__).parents[2] / "samples" / "refund-policy.md"`),
  `client.post("/documents/text", json={"text": text, "file_name": "refund-policy.md"})` → **201**; body
  `status == "cached"`; `"content" not in body`. (`FakeLlama.count_tokens` returns 50 < the 804-token limit
  under the `settings` fixture — `llama_ctx_size=1000 − answer_reserve=100 − prompt_overhead=96 = 804` — so it
  caches; mirrors the existing ingest roundtrip test.)
- `test_sample_files_are_nonempty_markdown` — both `samples/*.md` exist, are non-empty after `.strip()`, and
  contain the load-bearing conditional string (`assert "only if" in refund_text.lower()`) so a future edit
  can't silently drop the fact the Verify demo depends on.
- (Optional) `test_sample_query_roundtrip` — ingest the sample text then `POST /query {question:"…",
  document_id:id}` → 200 with the fake answer, proving the Chat hand-off path end to end.

**Invariants & risks:**
- **Invariant 1** — samples are ordinary documents ingested through the unchanged `POST /documents/text` →
  `_ingest` → `SYSTEM_TEMPLATE` path; nothing about the template changes.
- **Invariant 3** — sample ingest warms and saves like any document; the self-heal path is unchanged.
- **Invariant 5** — no endpoint or response changes; the button reuses existing routes.
- **Invariant 7** — pure Markdown files + one JS button + stdlib-only tests; no new dependency.
- **Failure mode — sample larger than the per-slot budget** (would 413 on small-context installs): avoided by
  keeping both files to a few hundred words; the token-limit test guard (via the fake) plus the authoring rule
  catch regressions.
- **Failure mode — embedded JS copy drifts from `samples/*.md`:** the on-disk files are the source of truth
  (README + tests reference them); `test_sample_files_are_nonempty_markdown` pins the conditional string so
  the demo-critical fact can't silently disappear. (A future nicety — serving the samples via the F9 static
  mount so the SPA fetches them — is out of scope here to keep F10 dependency-light.)
- **Failure mode — the Verify demo doesn't actually catch anything** (sample too vague): avoided by encoding
  an explicit conditional (*"refundable only if defective"*) that makes the naive claim *"refundable within 30
  days"* provably contradicted/conditional — exactly what Verify (and F1/F3) is built to surface.

**Acceptance (done when):**
- [ ] `samples/acme-widget-manual.md` and `samples/refund-policy.md` exist, are dense and self-contained, and
  each carries checkable facts; the policy carries an explicit conditional.
- [ ] On a fresh stack, `/ui` empty state shows **Try a sample**; one click ingests a sample to `cached` and
  lands in Chat with a suggested question pre-filled.
- [ ] Pasting *"Widgets are refundable within 30 days"* into Verify against the refund sample yields a
  **contradicted** (or conditional, post-F3) verdict — the promised catch.
- [ ] README "Use it" block names the samples; `test_sample_ingests_to_cached` and the Markdown-guard test
  pass; `ruff check --no-cache api` clean; `pytest api` green.

---

## 7. Build sequence & dependency graph

The eight in-scope features form a shallow dependency DAG with **three hard edges** and **two soft
(progressive-enhancement) edges**. Everything else parallelizes.

**Hard edges (a consumer cannot be *correct* without the producer):**
- **F1 → F2.** The gate's `pass` rule is `verdict=="supported" && quote_grounded===true`; `quote_grounded`
  only exists once `POST /verify` (F1) ships. Before F1 it is `undefined`, strict `=== true` yields `false`,
  and the gate fails *closed* — safe but useless (every draft escalates).
- **F1 → F3.** F3 is the `conditions` field inside the *one* verdict schema F1 defines
  (`DEFAULT_VERDICT_SCHEMA`). They co-build; F3 has no standalone surface.
- **F1 → F4 (soft-hard).** F4 reuses F1's `grounding()` for its fuzzy tiebreak, but ships an inline
  `_score_answer()` fallback so it is **independently buildable** if sequenced before F1 lands. Prefer
  F1-first; the build does not stall on it.

**Soft edges (a *feature within* a shipped feature lights up when the producer arrives):**
- **F1 → F9's Verify `quote_grounded` column** (SPA probes `/verify`, falls back to `/query`+schema).
- **F5 → F9's Stats savings line** and **F5 → `llamacag.py status` savings line** (Stats tab attempts
  `/stats`, omits the section on 404).
- **F9 → F10** (the "Try a sample" button lives in F9's empty state; samples + test land independently).

**The DAG:**
```
        F1 ──┬──► F2  (gate: needs quote_grounded)
             ├──► F3  (co-built: conditions field in F1's schema)
             └┄┄► F4  (reuses grounding(); inline fallback if not yet merged)

        F5 ┄┄► F9.Stats line  &  llamacag status savings line   (progressive)
        F1 ┄┄► F9.Verify quote_grounded column                  (progressive)

        F9 ──► F10  (Try-a-sample button; samples+test land independently)

        F6  (fully independent — CLI-only, touches no engine/endpoint)
```

**Phasing (respecting every edge, maximizing parallelism):**

| Phase | Features | Parallel with | Effort | Why here |
|---|---|---|---|---|
| **0 — foundation** | **F1 + F3** (co-built) | F5, F6, F4, F10-files | **M** (~2–3 dev-days: `grounding.py` + anchored-window matcher is the real work; `verify_claim` is thin over `query()`; F3 is XS) | Unblocks the most downstream edges (F2, F4-matcher, F9-Verify-column). Fixes the shared verdict schema once. |
| **1 — independents** | **F5**, **F6**, **F4** (against inline fallback *or* wait for Phase 0) | each other + Phase 0 | **F5 M, F6 M, F4 M** — three separate surfaces (db/main vs llamacag.py vs cag/main), parallelizable across agents | None touch F1's code. F5 & F6 share *zero* files with F1; F4 shares only non-overlapping module-level additions in `cag.py`/`main.py`/`config.py`. |
| **2 — composition & SPA** | **F2** (gate workflow), **F9** (web UI) | each other | **F2 S**, **F9 M** | F2 needs F1's `/verify` live. F9 is best after F1 (Verify column) and F5 (Stats line) so both progressive tabs light up on first ship rather than shipping half-dark. |
| **3 — first-run polish** | **F10** (samples + first-run) | — | **S** | Depends on F9's SPA to host the button; its richest Verify demo depends on F3's `conditions`. Genuinely last. |

**What parallelizes, concretely.** Phase 0 (F1/F3) and all of Phase 1 (F5, F6, F4-with-fallback) run as
**four concurrent agent lanes** — they share no source lines: F1 adds `grounding.py`+`verify_claim`, F5 adds
`usage_stats`+`/stats`, F6 lives entirely in `llamacag.py`, F4 adds `calibrate`+`/documents/{id}/calibrate`.
The only shared *files* are `cag.py`, `main.py`, `config.py`, `conftest.py`, `README.md`, `ROADMAP.md` — and
each appends distinct, non-overlapping blocks (new methods, new routes, new config knobs, new fake
attributes). The merge conflicts are trivial (adjacent additions), which the branch strategy (§10) sequences
to avoid.

**Realistic total:** ~8–11 dev-days serialized; ~4–5 calendar-days across 3–4 lanes, with Phase 2/3 (F2, F9,
F10) gated behind Phase 0's merge because they consume `/verify` and the SPA.

**Migration-ordering note the builder must honor.** Both **F4** (optional `reliability` column on
`documents`) and **F5's follow-up** (optional `cache_source` column on `query_log`) introduce the *first ever*
files under `database/migrations/` (the directory does not exist today — confirmed). Whichever lands first
**creates the directory**; to avoid two branches both writing `001_`, this plan assigns **F5 →
`001_cache_source.sql`** and **F4 → `002_reliability.sql`**. Both must be `ADD COLUMN IF NOT EXISTS`
(idempotent) **and** patch `database/schema.sql` for fresh volumes — the schema edit and the migration are two
separate edits that must agree.

---

## 8. Testing & CI

Every feature is proven the same way the stack already proves itself: against the in-memory fakes in
`api/tests/conftest.py` (`FakeLlama`, `FakeDatabase`) and the `httpx.MockTransport` fake in
`mcp/tests/conftest.py` (`FakeCagApi`) — **no Docker, no Postgres, no llama-server, no network**. The fakes
implement the exact engine-facing and HTTP-facing surface, so green `pytest api` / `pytest mcp` is a real
signal. The CLAUDE.md rule holds for every F#: **extend the fakes, never mock ad hoc.**

**Per-feature test approach**
- **F1 & F3 — grounding + `POST /verify`.** Three layers. (1) *Pure unit* `test_grounding.py` (no fakes) —
  exact/paraphrase/fabricated/empty/threshold-sweep + a 60k-word timing guard. (2) *Engine* `test_cag.py`
  drives `engine.verify_claim(...)` over the fakes, including `test_verify_reuses_query_prefix_byte_identical`
  which captures `fake_llama.last_messages[0]["content"]` from a plain `engine.query("hi")` and asserts the
  system message is byte-identical after `verify_claim`, plus `fake_llama.last_json_schema ==
  DEFAULT_VERDICT_SCHEMA`. (3) *Contract* `test_api.py` asserts the full status map (404/409/502/422, and
  non-JSON model answer → **200** `verdict:"error"`).
- **F2 — answer-gate workflow.** Zero core code ⇒ **zero Python tests**. Verification is the CI "Validate
  workflows" job auto-covering `answer-gate-workflow.json` plus a documented manual n8n import check.
- **F4 — calibration.** `test_cag.py` drives `engine.calibrate(...)` with the new **scripted-answer** fake:
  2-of-3 → `accuracy == round(2/3, 4)` with the miss listed; containment vs. `strict`; fuzzy tiebreak;
  unknown-doc raises *before any* `chat` (`fake_llama.called("chat") == []`); every item in `fake_db.queries`;
  `fake_db.documents[1]["reliability"]` set. `test_api.py` covers 200/404/422-empty/422-over-cap (naming
  `CALIBRATE_MAX_ITEMS`) with a tiny-cap client fixture.
- **F5 — `GET /stats`.** `test_db.py` (the file's `_one`-injection style): canned aggregate row →
  `reuse_ratio == round(900/1000, 4)`; no-token divide-by-zero guard; interval **bound not concatenated**
  (`params` is a 2-tuple with both elements equal, incl. `(None, None)`). `test_api.py`: `{windows, savings}`
  shape, money line hidden when price `0.0` / shown when set, `GET /` lists `"GET /stats"`. `test_cag.py`:
  pricing lives in the *engine* wrapper, not the DB fake.
- **F6 — `prepare` CLI.** `api/tests/test_prepare.py` (under `api/tests/` so `pytest api` collects it; a
  `sys.path` shim imports the stdlib-only root `llamacag`). Monkeypatches pypdf's `extract_text` and
  `subprocess.run`/`shutil.which`. Load-bearing: the text-layer path **never invokes the converter** (a
  sentinel `subprocess.run` fails the test if called); when a converter runs, the argv it receives is a
  **list** with `{in}`/`{out}` replaced by real paths.
- **F9 — web UI.** `test_api.py`: `client.get("/ui/")` → 200 text/html containing `id="view"`; a
  self-contained guard reading `index.html` asserting no `http(s)://` src/href; and, if the flag ships, a
  `webui_enabled=False` engine → `/ui/` returns 404. Frontend behavior is effort not risk — no unit test now.
- **F10 — samples + first-run.** `test_api.py`: read `samples/refund-policy.md`, `POST /documents/text` → 201
  `status:"cached"`, `"content" not in body`; a guard asserting both sample files are non-empty Markdown and
  the refund sample still contains the conditional (`assert "only if" in refund_text.lower()`).

**New fake methods / attributes needed** (all additive to `conftest.py`; existing tests keep passing because
defaults preserve today's behavior):

| Fake | Addition | Used by | Back-compat |
|---|---|---|---|
| `FakeLlama` | `answer_json: str \| None = None`; in `chat()`, `content = self.answer_json if self.answer_json is not None else self.answer` | F1 | default `None` ⇒ `answer="the answer"` tests unchanged |
| `FakeLlama` | `scripted: dict[str,str] = {}`; in `chat()`, key off the **last user message** (`messages[-1]["content"]`), fall back to `self.answer` | F4 | empty dict ⇒ `self.answer` still applies |
| `FakeDatabase` | `"reliability": None` in the `insert_document` dict (carried by `_public`) + `set_reliability(document_id, accuracy)` | F4 | additive key; consumers ignore unknown fields |
| `FakeDatabase` | `usage_stats()` returning the real `{24h,7d,all}` shape (sums over `self.queries`, collapsing time windows) | F5 | new method; window-differentiation is a live-DB concern out of scope for the fake |

The two `FakeLlama.chat()` extensions compose cleanly: `answer_json` (a fixed JSON string) and `scripted`
(per-question) coexist because `scripted` keys off the user turn while `answer_json`/`answer` is the fallback —
F1 and F4 tests never set both.

**The full CI gate list** (from `.github/workflows/ci.yml`, three jobs; every PR for every F# must pass all):
1. **`ruff check api`** (job `api`, line 19).
2. **`pytest api -q`** (job `api`, line 20) — `testpaths=["tests"]` under `api/`.
3. **`ruff check mcp`** (job `mcp`, line 32).
4. **`pytest mcp -q`** (job `mcp`, line 33).
5. **Workflow JSON validation** (job `validate`, lines 40–61): loads every `n8n/workflows/*.json`, rejects
   the deprecated set `{function, cron, executeCommand, readBinaryFile, writeBinaryFile}`, verifies every
   `connections` source **and** target resolves to a declared node `name`, prints `OK <path> (<n> nodes)`.
   Today **5 workflows** validate; F2 adds `answer-gate-workflow.json` (→6, expect `OK … (8 nodes)`); F4
   optionally adds `calibration-workflow.json` (→7). F1 *retargets* `claim-verification-workflow.json` (no
   new file).
6. **`docker compose config -q` ×3** (job `validate`, lines 62–70): base, `+gpu`, `+vulkan`, with
   `DB_PASSWORD`/`N8N_ENCRYPTION_KEY`/`N8N_USER_MANAGEMENT_JWT_SECRET=ci-only`. This is the machine check
   behind invariant 6: any new env knob (F5's `CLOUD_PRICE_PER_1K_INPUT`, F9's optional `WEBUI_ENABLED`) must
   keep all three renders valid.

**Reconciliation — `ruff check` vs `ruff check --no-cache`.** Feature acceptance lists assert `ruff check
--no-cache api`; CI runs plain `ruff check api` (a fresh runner has no cache anyway). Not in conflict —
`--no-cache` is the *local* pre-push habit guaranteeing a stale `.ruff_cache` can't mask a lint error, and it
produces byte-identical results to CI. **Do not change `ci.yml` to add `--no-cache`.** All new Python
satisfies the configured rule set `["E","F","W","I","UP","B"]` at `line-length = 100` (pyproject.toml:30–34).

**What the fakes deliberately do *not* cover** (so reviewers don't expect it): real KV-cache reuse, slot
save/restore against live llama-server, Postgres time-window correctness (`now() - interval`), n8n runtime
execution, and browser behavior. These are E2E concerns (still unverified per MEMORY.md) — the fakes prove the
*contract and control flow*, not that the engine reuses a prefix on real hardware. F5's window SQL and F4's
migration are the two widest gaps, and both call it out in-spec.

---

## 9. Invariants & risk register

**Per-PR reviewer checklist — the 7 binding invariants.** Not mergeable until every applicable box ticks
(N/A is valid when the feature provably doesn't touch that surface):

1. **Byte-identical `SYSTEM_TEMPLATE`.** ☐ No new content in the system prefix — new behavior rides in a
   **user turn** (F1/F4 prompts) or in **sampling** (`json_schema`/`temperature`, which `chat()` takes as
   arguments and never folds into a message, cag.py:339–346). ☐ Any feature that generates calls `query()`
   rather than build its own `messages`; F1's `verify_claim` and F4's `calibrate` both do, and
   `test_verify_reuses_query_prefix_byte_identical` pins it. **Red flag:** any diff editing
   `_system_message`/`SYSTEM_TEMPLATE`, or constructing a `messages` list outside `query()`.
2. **No shell in the request path; parameterized SQL only.** ☐ No `subprocess`/`os.system`/`shell=True`
   under `api/app/`. F6's converter *does* spawn a process but lives entirely in `llamacag.py`
   (`subprocess.run([...])` list-argv, never `shell=True`). ☐ Every new SQL statement (F4 `set_reliability`,
   F5 `usage_stats`) uses bound `%s` params — including the F5 interval, bound *twice*.
3. **Queries stay correct with no cache files.** ☐ No new code makes a missing `.bin` an error. F1/F4
   inherit `query()`'s self-heal untouched; verification grounds against `doc["content"]`, not cache state.
   **Red flag:** any early-return or raise keyed on cache-file absence.
4. **Two locks.** ☐ New engine methods take neither lock directly, or hold `_slots_guard` only momentarily
   and **never across I/O**. F4's `calibrate` composes over `query()` per item; F5's `usage_stats` is a pure
   `query_log` read. **Red flag:** a loop wrapped in a single long `_lock` hold, or any I/O under
   `_slots_guard`.
5. **API changes are additive.** ☐ No existing endpoint/field/shape changed or removed — only new endpoints
   (`/verify`, `/documents/{id}/calibrate`, `/stats`), new nullable fields (`reliability`), new index-list
   strings. ☐ New fields *present-but-null* where a consumer might branch (F5 `estimated_usd`). ☐
   `log_query`'s new `cache_source` (F5 follow-up) is a **trailing** keyword-default param and **trailing**
   SQL column, so the FK-retry `params[1:]` slice (db.py:158) still works. **Red flag:** a renamed key, a
   changed status code, or a positional-arg insertion into `log_query`.
6. **Config defaults agree in three places.** ☐ Every *model/context/deployment* env knob is in
   `config.py`, the `${VAR:-default}` fallback in **all three** `docker-compose*.yml` `cag-api` blocks, **and**
   `.env.example`. F5's `CLOUD_PRICE_PER_1K_INPUT` and F9's optional `WEBUI_ENABLED` follow this;
   `docker compose config -q` ×3 enforces it. ☐ *Behavioral-only* knobs `cag-api` reads but compose/geometry
   never share (F1 `quote_match_threshold`, F4 `calibrate_*`, F6 `PREPARE_*`) live in `config.py` (or `.env`
   for CLI-only F6) with a one-line comment saying why the rule does **not** apply. **Red flag:** a knob added
   to `config.py` but not the three compose files.
7. **`ruff`/`pytest`/workflow-valid/no-new-dep.** ☐ All CI green. ☐ **No new runtime dependency** — every
   feature uses stdlib (`difflib`, `re`, `json`, `subprocess`, `shlex`, `shutil`) or an already-declared dep
   (`fastapi.staticfiles`/`python-multipart` for F9, `pypdf` for F6's lazy import). **Red flag:** a new line
   in `[project.dependencies]`.

**Risk table**

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| **Byte-identical-prefix regression** (a feature slips content into the system prefix, silently invalidating every doc's KV cache) | Low | **Critical** — every cached doc self-heals slowly; users see minutes-long "recomputed" queries with no error | Structural: F1/F4 never build `messages` — they call `query()`. `test_verify_reuses_query_prefix_byte_identical` asserts byte-identity. Reviewer checklist item 1 flags any `_system_message`/`SYSTEM_TEMPLATE` edit or out-of-`query()` `messages` construction. |
| **Migrations on existing deployments** (`schema.sql` runs only on a fresh volume; F4/F5 columns absent on running stacks) | Medium | Medium — a live deploy that pulls the new image but skips the migration hits "column does not exist" | New columns ship in **both** `schema.sql` (fresh) **and** `database/migrations/00x_*.sql` (existing, hand-applied), each idempotent `ADD COLUMN IF NOT EXISTS`. The engine degrades gracefully — F4's `set_reliability` behind `hasattr`, F5's pre-migration rows have `NULL cache_source` (fall outside `FILTER` counts). Forward-only, non-destructive, safe to re-run. README "Updating & maintenance" documents the one-time step. |
| **New-dependency creep** (a fuzzy-match lib, a PDF-to-Markdown lib, a chart lib) | Medium | Medium — violates invariant 7; grows image + supply-chain surface | Hard budget: stdlib or already-declared deps only. F1's grounding is `difflib`+`re`; F6's converter is *pluggable and external* (`PREPARE_CMD`, user-installed) so the repo takes no dep on marker/docling; F9 uses `fastapi.staticfiles` (present). Reviewer item 7 blocks any `[project.dependencies]` addition; CI's `pip install -e` + `pytest` surfaces an undeclared import immediately. |
| **Unauthenticated web-UI exposure** (F9 at `/ui`; binding the API beyond `127.0.0.1` to reach it from a phone publishes an unauthenticated document store + inference endpoint) | Medium | **High** — anyone on the network can read/delete documents and run inference | Loopback is the security boundary (ports bind `127.0.0.1`, docker-compose.yml:64). F9 mounts at a sub-path (never `/`) and ships a security-boundary paragraph verbatim in README + `index.html`, pointing multi-user needs at **F8**. Optional `WEBUI_ENABLED=false` disables the surface. No auth is *added* here (doing so silently would be worse — false security); the mitigation is making the boundary loud. |
| **Converter privacy** (F6: a cloud vision converter in `PREPARE_CMD` silently ships confidential documents to a third party) | Medium | **High** — a scanned contract leaves the machine without the user realizing | `prepare` prefers the **local, offline** text-layer path first and never shells out when a PDF has a text layer. When a converter is needed, the guided error + `.env.example` present **local-first** options ("document never leaves your machine") and clearly label the cloud path as "the document **is sent** to a third party — do not use for confidential material." The README states the trade-off and mandates human review of the produced `.md`. F6 does **not** auto-ingest. |
| **Threshold miscalibration** (F1 `quote_match_threshold` / F4 `calibrate_match_threshold` too loose lets near-miss fabrications pass, too strict inflates review) | Medium | Medium — erodes trust either way | Documented, **independently-tunable** thresholds (F1 and F4 separate so quote-grounding and calibration strictness move independently). Behavior pinned by tests at 0.5/0.9/0.99; the response echoes `match_ratio`/`grounding_method` + misses. Docs state grounding checks **existence, not entailment**, and cannot harden `absent` — so `quote_grounded:True` is never sold as "the claim is true." |
| **Fuzzy-grounding cost on large docs** (F1: naive O(n²) match over 60k tokens per verify) | Low | Medium — verify latency spikes; F2's chained gate compounds it | Exact-substring fast path is O(n); the fuzzy path uses anchored windows (≥4-char anchors, capped hit count) with `quick_ratio()` pre-filtering. `test_large_document_stays_fast` guards a 60k-word doc under a ~2 s ceiling. |
| **Test collected by no CI job** (F6's `test_prepare.py` for the root `llamacag` module; `pytest api` and `pytest mcp` both use `testpaths=["tests"]`) | Low | Medium — a whole feature ships untested | Place `test_prepare.py` under `api/tests/` (inside the `api` job's `testpaths`) with a `sys.path` shim importing the stdlib-only `llamacag`. No `ci.yml` change, no new dep; `ruff check api` lints it. Acceptance requires `pytest api -q` to show the cases. |

**Grounding note (facts verified against the code, not assumed):** the CI gate list, deprecated-node set, and
triple-compose check are from `.github/workflows/ci.yml` (lines 19–70); the HTTP status map (415/413/409/404/
502) and `index()` endpoint list are from `api/app/main.py` (82–127); `log_query`'s trailing-param signature
and the `params[1:]` FK-retry slice are `api/app/db.py` (126–158); `query_log` has **no** `cache_source`
column and `documents` has **no** `reliability` column today (`database/schema.sql` 27–42) — confirming both
migrations are genuinely additive; `cag.py` (349, 376–385) computes `cache_source` at query time but does
**not** persist it (the gap F5's follow-up closes); `testpaths=["tests"]` in both `api/pyproject.toml` and
`mcp/pyproject.toml` confirms the F6 test-collection risk; and `database/migrations/` does not yet exist — F4/
F5 create it. Five workflows exist today (query, document-ingestion, maintenance, question-sweep,
claim-verification); F2 adds the sixth.

---

## 10. Branch / PR / rollout strategy

**One long-lived feature branch off `main`, phased commits, `main` untouched until the whole thing is green
and reviewed.** `main` is clean at `c08d14b`; nothing in flight competes.

**Branch name:** `feat/oracle-hardening-and-webui` — the two themes the reviews prioritized (make the oracle
honest: F1–F5; give it a zero-install face: F6/F9/F10). Cut from `main`:
```
git switch -c feat/oracle-hardening-and-webui main
```

**Commit-per-phase, one logical unit each** (so review reads as a story and any phase can be reverted alone):
1. `feat(verify): mechanical quote-grounding + POST /verify with conditions scope (F1, F3)` — `grounding.py`,
   `verify_claim`, `DEFAULT_VERDICT_SCHEMA`, endpoint, retargeted `claim-verification-workflow.json`, tests,
   README oracle asymmetry + `conditions`.
2. `feat(stats): usage & cost-savings observability GET /stats (F5)` — `usage_stats`, `/stats`, `status`
   one-liner, `CLOUD_PRICE_PER_1K_INPUT` in the three places, fake + tests. *(If taking the `cache_source`
   follow-up, land it as its own commit carrying `database/migrations/001_cache_source.sql` + the `schema.sql`
   edit + the `log_query`/`cag.py` gap-close.)*
3. `feat(prepare): offline PDF/scan→Markdown CLI helper (F6)` — `cmd_prepare`, subparser, `PREPARE_*` in
   `.env.example`, `api/tests/test_prepare.py`, README.
4. `feat(calibrate): per-canon reliability battery POST /documents/{id}/calibrate (F4)` — `calibrate`,
   `_score_answer`, endpoint, `calibrate_*`, tests; optional `reliability` column as a **separable trailing
   commit** carrying `database/migrations/002_reliability.sql` + `schema.sql` + `db.py`/fake, so the column can
   be dropped from the PR without unpicking the core feature.
5. `feat(gate): answer-gate workflow + fail-safe pattern docs (F2)` — `answer-gate-workflow.json`, README
   "Gating a support bot's answers". *(After commit 1 so `/verify` exists.)*
6. `feat(webui): zero-install SPA served at /ui (F9)` — `api/app/webui/index.html`, the `main.py` mount,
   `pyproject.toml` package-data, optional `WEBUI_ENABLED` in the three-places, smoke tests. *(After 1 and 2
   so Verify column + Stats line are live.)*
7. `feat(samples): curated samples + guided first-run (F10)` — `samples/*.md`, empty-state button in
   `index.html`, ingest tests, README "Use it" line. *(Last; depends on 6.)*

Each commit ends with the `Co-Authored-By: Claude <noreply@anthropic.com>` trailer per repo convention, and
each is **self-green**: `ruff check --no-cache api mcp`, `pytest api -q`, `pytest mcp -q`, the CI
workflow-validation script over `n8n/workflows/*.json`, and `docker compose config -q` (×3) all pass *at that
commit*, not just at branch tip. This matters because the CI `validate` job runs `docker compose config -q`
for all three variants — any compose knob added out-of-sync (F5's `CLOUD_PRICE_PER_1K_INPUT`, F9's optional
`WEBUI_ENABLED`) fails the whole PR, so the three-places edit must be *in the same commit* as the code that
reads the knob.

**CI-green-before-merge, `main` protected.** Open the PR early as a **draft** so CI runs on every push; flip
to ready only when all three CI jobs (api, mcp, validate) are green at tip. The three-places invariant and the
workflow whitelist are *machine-checked* by the `validate` job — lean on that rather than manual review for
those two.

**Keeping it reviewable despite the size** (~15 files across two languages):
- **Phase commits are the review unit.** A reviewer reads commit 1 (the oracle hardening — the highest-stakes
  change, since it touches the KV-reuse invariant) in isolation, signs off, then moves on. The `git log` reads
  as the roadmap.
- **Separate the risky invariant-touch from everything else.** Commit 1 is the *only* commit that goes near
  `query()`/`SYSTEM_TEMPLATE`; call it out in the PR description and point the reviewer at
  `test_verify_reuses_query_prefix_byte_identical`. Everything downstream (F2, F5, F9, F10) is either pure
  workflow/SPA/CLI or additive read-only endpoints — much lower stakes.
- **Migrations flagged in the PR body.** Because F4/F5's columns are the first-ever migrations, the PR
  description must include the one-time `docker compose exec -T db psql … < database/migrations/00N_*.sql`
  commands for existing deployments and state plainly: *fresh volumes get it from `schema.sql`; existing
  volumes need the migration run by hand.* The reviewer confirms `schema.sql` and the migration agree.
- **The optional columns are behind separable commits**, so a reviewer can defer them by dropping two commits,
  not surgery inside a feature.
- **One PR, not seven.** The features share the README oracle section, the verdict schema, and the fakes;
  splitting into seven PRs would create a merge-order tangle (F2's PR can't be green until F1's is merged). A
  single phased PR with clean per-phase commits is more reviewable than a dependency chain of small PRs. If the
  branch grows unwieldy, the natural split is **Phase 0–1 as PR-A** and **Phase 2–3 off PR-A's merge as PR-B**
  — but start as one and split only if asked.

**Rollout after merge:** all seven features are **off-by-default-safe or additive** — `/verify`, `/stats`,
`/calibrate` are new endpoints nobody calls until pointed at; the SPA is gated by `_webui_dir.is_dir()` (and
optionally `WEBUI_ENABLED`); `prepare` is a new subcommand; samples are inert files. Nothing changes existing
behavior, so merge-to-`main` needs no staged rollout beyond running the two optional migrations on any
pre-existing deployment. Tag the merge (e.g. `v2.1`) since it's a coherent capability jump.

---

## 11. For the reviewer — open decisions

These are the forks the specs deliberately leave open. Each needs a yes/no before or during the build; none
blocks branch-cut.

**1. F9 web UI: hand-written static SPA vs. Gradio/Streamlit.** The plan commits to a **self-contained
vanilla-JS `index.html`** mounted via Starlette `StaticFiles` — *zero new runtime dependency* (StaticFiles
ships with FastAPI, `python-multipart` is already installed), *same-origin so no CORS*, one `start`/one URL,
matching the dark palette and self-contained-SVG convention. **Cost:** someone hand-writes ~1 file of
HTML/CSS/JS (tabs, drag-drop, poll loop) — effort, not risk. The alternative (a **Gradio/Streamlit app in a
`webui/` container**) lets you write Python and reaches parity faster, but costs *a new service, a new heavy
dependency, extra RAM, and cross-origin calls* — breaking the "no new dependency / one process / footprint"
thesis the feature rests on. **Recommendation:** static SPA. **Human owns:** whether hand-writing the frontend
is an acceptable use of effort, or the team would rather pay the footprint cost. The single biggest scope lever
— pick before Phase 2.

**2. F4 and F5's optional DB columns: migrate now, or defer.** Both features work *fully* without their
column: F5's `/stats` computes every aggregate from existing `query_log` columns; F4's `calibrate` returns the
score in its response and only *persists* it if `set_reliability` exists (behind `hasattr`). The columns add:
F5 → a memory/disk/recomputed *residency distribution* on `/stats` (and closes the gap that `cag.py`
computes `cache_source` at query time but never persists it); F4 → `reliability` on `GET /documents`. **The
cost of "now":** these are the **first-ever migrations** in the repo, so shipping them establishes the
migration *pattern and discipline* — every existing deployment must run `psql … < 00N_*.sql` by hand because
`schema.sql` only runs on a fresh volume. **Recommendation:** ship the **no-column versions first** (both
specs are written to), take the columns as **separable trailing commits** the reviewer can keep or drop; if
kept, use the assigned numbering (F5 `001_cache_source.sql`, F4 `002_reliability.sql`). **Human owns:** are we
ready to own migrations (and the manual-apply burden) in this PR, or defer both columns?

**3. F6 converter default: cloud vs. local, and whether to default one at all.** F6 ships `PREPARE_CMD`
**unset by default**. The text-layer path needs *no* converter and never shells out; only scanned/image/chart
PDFs hit the converter branch, which today prints a **guided error** naming both local (marker/docling/local
vision — document never leaves the machine) and cloud (faster, *but the document is sent out*) options.
**Recommendation:** keep `PREPARE_CMD` **empty by default** with the guided error; never auto-configure a
cloud converter (that would silently exfiltrate documents). **Human owns:** whether to pre-fill a *local*
default like `marker` (better first-run, but forces `pip install marker-pdf` on users who never touch a
scanned PDF) or keep it opt-in.

**4. F2: ship as workflow-only, or add a `/gate` endpoint.** The plan ships F2 as **pure n8n orchestration**
(zero core code, zero new tests) — the gate logic stays visible/editable by non-technical operators and adds
no API surface. The alternative — a `POST /gate {question, draft, document_id?}` endpoint — would be
unit-testable with `FakeLlama` and callable outside n8n, but adds an endpoint whose only logic is "call two
existing endpoints and apply a boolean," which the workflow already expresses; the ROADMAP explicitly says to
promote to an endpoint *only when the Set-node expression is no longer enough*. **Recommendation:**
workflow-only for now. **Human owns:** whether the team wants a testable/reusable `/gate` endpoint despite the
thin logic.

**5. Gate the web UI behind `WEBUI_ENABLED` — and if so, default on or off?** F9 can mount `/ui`
**unconditionally** (whenever `api/app/webui/` exists — the `_webui_dir.is_dir()` guard already prevents a boot
crash) or **behind a `WEBUI_ENABLED` flag** wired through the three-places rule. A flag lets a security-conscious
operator turn the browser face off entirely (defense-in-depth on an unauthenticated, loopback-bound stack), at
the cost of one more knob to keep in sync across three compose variants (the CI `validate` job fails the PR if
they drift). The counter-argument: the UI is *already* loopback-only and carries a prominent security-boundary
warning; a flag defaulting to `true` adds ceremony without changing the default posture. **Recommendation:**
add `WEBUI_ENABLED` **defaulting to `true`** — cheap insurance, and the specs already carry the three-places
edits and a `test_webui_disabled_returns_404` guard. **Human owns:** (a) flag or no flag, and (b) if flag,
default `true` (convenience) or `false` (opt-in — safer but hurts the zero-install first-run that is the entire
point of F9). Note the tension: defaulting `false` partly defeats "run `start`, open a URL, done."
