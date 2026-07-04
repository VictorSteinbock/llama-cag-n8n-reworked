"""CAG orchestration: N inference slots, N "hot" documents at a time.

llama-server is started with --parallel CAG_SLOTS and divides its context
evenly between slots; the engine assigns each document to a slot (LRU eviction
when all are busy), so up to CAG_SLOTS documents keep their KV state resident
in RAM and switching between them costs nothing.

The contract with llama-server that makes caching work:

1. A document's system message is byte-identical at warm time and at every
   query, so the templated prompt shares its long prefix with what is already
   in its slot's KV cache (`cache_prompt: true` skips re-evaluating it).
2. A slot's KV state is persisted to disk after warming (slot_save) and
   restored (slot_restore) whenever an evicted document becomes hot again —
   also after container restarts.

If a cache file is missing or restore fails, queries still succeed: llama-server
recomputes the prefix (slow once), and the engine re-saves the slot so the next
query is fast again. A llama-server restart behind our back degrades the same
way — the first query recomputes, correctness is never affected.
"""

import hashlib
import json
import logging
import re
import threading
import time
from collections.abc import Callable

from .config import Settings
from .db import Database
from .extract import extract_text
from .grounding import grounding, recall_probe
from .llama import LlamaClient, LlamaError

logger = logging.getLogger(__name__)

SYSTEM_TEMPLATE = (
    "You are a precise assistant. Answer questions using only the information in the "
    "document below. If the document does not contain the answer, say so plainly.\n\n"
    '<document name="{name}">\n{content}\n</document>'
)

# The ~20-token warm turn is the deliberate price of model-agnostic warming.
WARM_USER_MESSAGE = "Reply with the single word: ready"

# The single, shared verdict schema for POST /verify. F2 (answer-gate), F4
# (calibration) and F9 (Verify tab) reference this by name — never redefine it.
# The `conditions` field (F3) surfaces a scope the document places on the claim
# (e.g. "only if defective") so a conditional isn't mislabeled as unconditional.
DEFAULT_VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "claim": {"type": "string"},
        "verdict": {"enum": ["supported", "absent", "contradicted"]},
        "quote": {"type": "string"},
        "conditions": {"type": "string"},
    },
    "required": ["claim", "verdict", "quote", "conditions"],
}

VERIFY_PROMPT_TEMPLATE = (
    'Verify this claim strictly against the document: "{claim}". '
    "Give your verdict (supported, contradicted, or absent), the exact verbatim "
    'supporting or contradicting passage as "quote" (empty string if absent), and in '
    '"conditions" any scope or condition the document places on the claim (empty '
    "string if it applies unconditionally)."
)


class DocumentTooLargeError(Exception):
    def __init__(self, n_tokens: int, limit: int, ctx_size: int, slots: int) -> None:
        self.n_tokens = n_tokens
        self.limit = limit
        self.ctx_size = ctx_size
        self.slots = slots
        per_slot = f" ÷ CAG_SLOTS={slots}" if slots > 1 else ""
        super().__init__(
            f"Document is {n_tokens} tokens but the per-slot limit is {limit} "
            f"(LLAMA_CTX_SIZE={ctx_size}{per_slot}, minus answer + prompt head-room). "
            "Raise LLAMA_CTX_SIZE in .env and restart, or ingest a smaller document."
        )


class NoCachedDocumentError(Exception):
    pass


class UnknownDocumentError(Exception):
    pass


def _slugify(file_name: str) -> str:
    stem = re.sub(r"\.[A-Za-z0-9]+$", "", file_name)
    slug = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")
    return slug[:80] or "document"


def _estimate_turn_tokens(content: str) -> int:
    """Cheap turn-size estimate for history budgeting: ~3 chars per token plus
    a small per-turn allowance for the chat template's role framing."""
    return len(content) // 3 + 8


def _normalize_answer(text: str) -> str:
    return " ".join(text.split()).casefold()


def _score_answer(got: str, expected: str, *, strict: bool, threshold: float) -> bool:
    """Did the answer match the expected value? Containment-first: a correct short
    answer ("12 A") embedded in a verbose reply must score correct, which a
    whole-string ratio would miss. Below containment, grounding()'s anchored-window
    fuzzy match (from F1) tolerates spacing/format drift ("150C" vs "150 C")."""
    exp, ans = _normalize_answer(expected), _normalize_answer(got)
    if not exp:
        return False  # empty expected is a spec error, never a pass
    if strict:
        return ans == exp
    if exp in ans:  # normalized containment: the primary signal
        return True
    return grounding(expected, got, threshold=threshold)["match_ratio"] >= threshold


class CagEngine:
    def __init__(self, llama: LlamaClient, db: Database, settings: Settings) -> None:
        self._llama = llama
        self._db = db
        self._settings = settings
        # Two locks, two jobs.
        #
        # _lock serializes slot USE: restore + completion must be atomic, and
        # CPU inference is sequential anyway. Concurrent requests queue here —
        # possibly for minutes during a warm.
        #
        # _slots_guard is a momentary micro-lock protecting only the slot-map
        # dicts (_slots/_slot_used) so readers that must never queue behind a
        # generation (health) can snapshot them. Writers take it nested inside
        # _lock for the map mutation lines only; it is never held across I/O.
        self._lock = threading.Lock()
        self._slots_guard = threading.Lock()
        self._slots: dict[int, int] = {}  # slot_id -> document_id currently hot
        self._slot_used: dict[int, float] = {}  # slot_id -> monotonic last use
        # Deferred-work runner: the healed-query re-save runs through this so
        # the response doesn't wait for slot_save. Tests replace it with an
        # inline runner (lambda fn: fn()) for determinism.
        self._spawn: Callable[[Callable[[], None]], None] = (
            lambda fn: threading.Thread(target=fn, daemon=True).start()
        )
        # Model-fingerprint check state: performed lazily on the first
        # successful llama interaction per process (see _ensure_model_marker).
        self._model_checked = False

    # --- helpers -------------------------------------------------------------

    @property
    def settings(self) -> Settings:
        """Read-only view for the HTTP layer (e.g. the upload cap)."""
        return self._settings

    def _system_message(self, file_name: str, content: str) -> dict:
        return {
            "role": "system",
            "content": SYSTEM_TEMPLATE.format(name=file_name, content=content),
        }

    @staticmethod
    def _cache_filename(document_id: int) -> str:
        return f"doc-{document_id}.bin"

    def _ensure_model_marker(self) -> None:
        """Invalidate caches when the served model changed behind our back.

        llama.cpp validates a restored state file *structurally* (layer count,
        KV types, geometry) but stores no identity of the WEIGHTS that produced
        it — so switching between same-geometry models (a different weight
        quant of the same repo, a fine-tune of the same base) would restore
        stale KV state silently and answer from the wrong model's reading.

        Guard: on the first successful llama interaction per process, compare
        llama-server's /props model_path with the model.marker file next to
        the caches. On mismatch, delete every *.bin (they self-heal on next
        use) and write the new marker. Called under _lock, before any restore
        or warm touches a slot. If llama-server is down the check is simply
        deferred to the next interaction — never an error.
        """
        if self._model_checked:
            return
        try:
            props = self._llama.props()
        except LlamaError:
            return  # llama down/unreachable — retry on the next interaction
        identity = str(props.get("model_path") or "").strip()
        if not identity:
            # /props no longer exposes model_path (upstream change?): we cannot
            # fingerprint, so fail open — but say so once, loudly.
            logger.warning(
                "llama-server /props has no model_path; cannot verify cached KV "
                "state belongs to the current model. Restores of stale caches "
                "after a same-geometry model switch would go undetected."
            )
            self._model_checked = True
            return

        marker = self._settings.cache_dir / "model.marker"
        previous: str | None = None
        if marker.exists():
            try:
                previous = marker.read_text(encoding="utf-8").strip()
            except OSError as exc:
                logger.warning("Could not read model marker: %s", exc)

        if previous is not None and previous != identity:
            removed = 0
            for path in self._settings.cache_dir.glob("*.bin"):
                try:
                    path.unlink()
                    removed += 1
                except OSError as exc:
                    logger.warning("Could not remove stale cache %s: %s", path, exc)
            logger.warning(
                "Model changed (%s -> %s): invalidated %s cache file(s); "
                "they re-warm themselves on next use.",
                previous, identity, removed,
            )
        try:
            marker.write_text(identity, encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not write model marker: %s", exc)
            return  # leave unchecked so a later interaction retries
        self._model_checked = True

    def _token_limit(self) -> int:
        return (
            self._settings.slot_ctx_size
            - self._settings.answer_reserve_tokens
            - self._settings.prompt_overhead_tokens
        )

    def _slot_for(self, document_id: int) -> tuple[int, bool]:
        """Slot assignment: the doc's current slot, else a free one, else LRU."""
        for slot, hot_id in self._slots.items():
            if hot_id == document_id:
                return slot, True
        for slot in range(max(self._settings.cag_slots, 1)):
            if slot not in self._slots:
                return slot, False
        lru = min(self._slot_used, key=self._slot_used.__getitem__)
        return lru, False

    # --- ingest --------------------------------------------------------------

    def ingest_file(self, file_name: str, data: bytes) -> dict:
        return self._ingest(file_name, extract_text(file_name, data))

    def ingest_text(self, file_name: str, text: str) -> dict:
        return self._ingest(file_name, text.strip())

    def _ingest(self, file_name: str, text: str) -> dict:
        sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
        existing = self._db.find_by_sha256(sha256)
        if existing is not None:
            logger.info("Ingest of %s deduplicated to document %s", file_name, existing["id"])
            return {**existing, "deduplicated": True}

        n_tokens = self._llama.count_tokens(text)
        limit = self._token_limit()
        if n_tokens > limit:
            raise DocumentTooLargeError(
                n_tokens, limit, self._settings.llama_ctx_size, self._settings.cag_slots
            )

        doc = self._db.insert_document(_slugify(file_name), file_name, text, sha256)
        if doc is None:
            # Lost a concurrent-insert race on content_sha256: the winner's row
            # exists now — hand it back exactly like the normal dedupe path.
            existing = self._db.find_by_sha256(sha256)
            if existing is None:
                raise RuntimeError(
                    "Concurrent ingest of identical content conflicted, but the "
                    "winning row is already gone (deleted immediately?). Retry the ingest."
                )
            logger.info(
                "Ingest of %s lost an insert race; deduplicated to document %s",
                file_name, existing["id"],
            )
            return {**existing, "deduplicated": True}
        try:
            warm = self._warm(doc["id"], file_name, text)
        except LlamaError as exc:
            self._db.mark_failed(doc["id"], str(exc))
            raise
        self._db.mark_cached(doc["id"], n_tokens, self._cache_filename(doc["id"]))
        refreshed = self._db.get_document(doc["id"])
        return {**refreshed, "deduplicated": False, "warm_ms": warm.get("warm_ms")}

    def _warm(self, document_id: int, file_name: str, content: str) -> dict:
        """Fill the assigned slot with the document's KV state, persist to disk."""
        started = time.monotonic()
        with self._lock:
            self._ensure_model_marker()
            slot, _ = self._slot_for(document_id)
            try:
                self._llama.slot_erase(slot)
            except LlamaError:
                # An un-erasable slot only costs prefix-match efficiency once.
                logger.warning("slot_erase(%s) failed before warming doc %s", slot, document_id)
            self._llama.chat(
                [
                    self._system_message(file_name, content),
                    {"role": "user", "content": WARM_USER_MESSAGE},
                ],
                max_tokens=4,
                temperature=0.0,
                slot_id=slot,
                warm=True,
            )
            self._llama.slot_save(self._cache_filename(document_id), slot)
            with self._slots_guard:
                self._slots[slot] = document_id
                self._slot_used[slot] = time.monotonic()
        warm_ms = int((time.monotonic() - started) * 1000)
        logger.info("Warmed document %s into slot %s in %sms", document_id, slot, warm_ms)
        return {"warm_ms": warm_ms}

    # --- query ---------------------------------------------------------------

    def query(
        self,
        question: str,
        document_id: int | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        history: list[dict] | None = None,
        json_schema: dict | None = None,
    ) -> dict:
        if document_id is not None:
            doc = self._db.get_document(document_id, with_content=True)
            if doc is None:
                raise UnknownDocumentError(f"No document with id {document_id}")
        else:
            doc = self._db.latest_cached(with_content=True)
            if doc is None:
                raise NoCachedDocumentError(
                    "No cached documents yet — ingest one first "
                    "(drop a file in the documents folder or POST /documents)."
                )

        max_tokens = max_tokens or self._settings.default_max_answer_tokens
        temperature = (
            temperature if temperature is not None else self._settings.default_temperature
        )
        started = time.monotonic()

        # Long conversations must still fit the slot: trim the OLDEST turns
        # first until document + history + question fit the estimated budget.
        # The current question is never trimmed.
        history = list(history) if history else []
        history_trimmed = 0
        if history:
            budget = (
                self._settings.slot_ctx_size
                - (doc.get("n_tokens") or 0)
                - self._settings.answer_reserve_tokens
                - self._settings.prompt_overhead_tokens
            )
            used = _estimate_turn_tokens(question) + sum(
                _estimate_turn_tokens(turn.get("content", "")) for turn in history
            )
            while history and used > budget:
                dropped = history.pop(0)
                used -= _estimate_turn_tokens(dropped.get("content", ""))
                history_trimmed += 1
            if history_trimmed:
                logger.info(
                    "Trimmed %s oldest history turn(s) to fit document %s's slot budget",
                    history_trimmed, doc["id"],
                )

        # History sits between the (cached) document prefix and the new
        # question; identical earlier turns are prefix-matched in the KV cache,
        # so each round only evaluates the newest exchange.
        # NOTE: json_schema constrains sampling only — it is passed to chat()
        # below, never folded into a message, so the system prefix stays
        # byte-identical and the KV cache is reused exactly as before.
        messages = [
            self._system_message(doc["file_name"], doc["content"]),
            *history,
            {"role": "user", "content": question},
        ]
        try:
            with self._lock:
                slot, cache_source = self._make_hot(doc)
                healed = cache_source == "recomputed"
                result = self._llama.chat(
                    messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    slot_id=slot,
                    warm=healed,  # recomputing a full prefix deserves the long timeout
                    json_schema=json_schema,
                )
        except LlamaError as exc:
            self._db.log_query(
                document_id=doc["id"], question=question, answer=None,
                success=False, error=str(exc),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            raise
        if healed:
            # Deferred self-heal: answer now, persist in the background. The
            # deferred body re-takes the lock and re-validates the slot still
            # holds this document before saving.
            self._schedule_resave(doc, slot)

        duration_ms = int((time.monotonic() - started) * 1000)
        timings = result["timings"]
        usage = result["usage"]
        self._db.touch_used(doc["id"])
        self._db.log_query(
            document_id=doc["id"],
            question=question,
            answer=result["content"],
            success=True,
            n_prompt_tokens=usage.get("prompt_tokens"),
            n_cached_tokens=timings.get("cache_n"),
            n_eval_tokens=timings.get("prompt_n"),
            duration_ms=duration_ms,
        )
        timings_out = {
            "prompt_tokens_evaluated": timings.get("prompt_n"),
            "prompt_tokens_from_cache": timings.get("cache_n"),
            "answer_tokens": timings.get("predicted_n"),
            # "memory": doc was already hot; "disk": KV state restored from
            # its cache file; "recomputed": self-heal path, prefix was
            # re-evaluated (and re-saved) because no usable cache existed.
            "cache_source": cache_source,
        }
        if history_trimmed:
            # Present only when trimming occurred: how many oldest turns were
            # dropped to fit the slot budget.
            timings_out["history_trimmed"] = history_trimmed
        return {
            "answer": result["content"],
            "document": {
                "id": doc["id"],
                "file_name": doc["file_name"],
                "n_tokens": doc["n_tokens"],
            },
            "duration_ms": duration_ms,
            "timings": timings_out,
        }

    def verify_claim(
        self, claim: str, document_id: int | None = None, max_tokens: int | None = None
    ) -> dict:
        """Verify a claim against a document, then mechanically ground the quote.

        The generation goes through the existing ``query()`` verbatim: the verify
        instruction rides in a *user* turn and ``DEFAULT_VERDICT_SCHEMA`` is passed
        as the sampling schema, so the cached document prefix stays byte-identical
        (invariant 1). ``grounding()`` then confirms the model's ``quote`` actually
        occurs in the source bytes.

        Asymmetry worth stating plainly: grounding **hardens**
        ``supported``/``contradicted`` (there is a passage to check) but **cannot
        ground** ``absent`` (``quote_grounded=None``) — and it verifies the quote's
        *existence*, not the claim's *entailment*. ``absent`` therefore gets the
        mechanical ``recall_probe`` instead (the ``recall`` response field): near-zero
        overlap corroborates the verdict with an auditable number; high overlap says
        the canon *does* discuss this vocabulary, so downstream gates should escalate
        rather than quietly accept "absent". That residual gap is why F4
        (calibration) and F2 (answer-gating) exist.
        """
        question = VERIFY_PROMPT_TEMPLATE.format(claim=claim)
        result = self.query(
            question,
            document_id=document_id,
            max_tokens=max_tokens or self._settings.default_max_answer_tokens,
            temperature=0.0,
            json_schema=DEFAULT_VERDICT_SCHEMA,
        )
        # Ground against the exact cached bytes. query()'s return carries only
        # {id, file_name, n_tokens}, so re-fetch the content; that re-fetch can
        # race a concurrent delete (get_document -> dict | None), so guard it —
        # a vanished document is a 404, never a 500.
        resolved_id = result["document"]["id"]
        doc = self._db.get_document(resolved_id, with_content=True)
        if doc is None:
            raise UnknownDocumentError(f"Document {resolved_id} was deleted")

        # A schema-constrained answer is a JSON object; anything else (non-JSON,
        # or valid JSON that isn't an object) collapses to verdict "error" so the
        # endpoint stays 200 and the response shape is stable.
        try:
            parsed = json.loads(result["answer"])
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            verdict = parsed.get("verdict")
            quote = parsed.get("quote", "")
            conditions = parsed.get("conditions", "")
            # Coerce non-string quote/conditions (a schema slip could yield a
            # number/bool/list): a non-str quote reaching grounding() would call
            # .strip() on an int and 500. Non-strings collapse to "".
            if not isinstance(quote, str):
                quote = ""
            if not isinstance(conditions, str):
                conditions = ""
        else:
            verdict, quote, conditions = "error", "", ""
        if verdict not in {"supported", "absent", "contradicted"}:
            verdict = "error"

        if verdict != "error":
            g = grounding(quote, doc["content"], threshold=self._settings.quote_match_threshold)
        else:
            g = {"grounded": None, "match_ratio": 0.0, "method": "absent"}

        # "absent" has no quote to ground, so corroborate it mechanically instead:
        # does the claim's vocabulary co-occur anywhere in the canon? (Pure string
        # work on the already-fetched content — no second model call.)
        recall = recall_probe(claim, doc["content"]) if verdict == "absent" else None

        return {
            "claim": claim,
            "verdict": verdict,
            "quote": quote,
            "conditions": conditions,
            "quote_grounded": g["grounded"],
            "match_ratio": g["match_ratio"],
            "grounding_method": g["method"],
            "recall": recall,
            "document": result["document"],
            "duration_ms": result["duration_ms"],
            "timings": result["timings"],
        }

    def calibrate(
        self, document_id: int, qa: list[dict], *, strict: bool = False,
        max_tokens: int | None = None,
    ) -> dict:
        """Run a known-answer Q/A battery against a document at temperature 0 and
        score each answer, returning {n, correct, accuracy, misses}.

        Measures *this* canon under *this* model — the escalation rate you should
        expect before trusting the oracle on it. Composes over query() per item
        (builds no prompt of its own, so SYSTEM_TEMPLATE and KV reuse are
        untouched, invariant 1), and query() releases _lock between items so a
        long battery never starves health() (invariant 4). The 404 check happens
        before any generation (fail fast)."""
        doc = self._db.get_document(document_id)
        if doc is None:
            raise UnknownDocumentError(f"No document with id {document_id}")
        threshold = self._settings.calibrate_match_threshold
        correct, misses = 0, []
        for item in qa:
            question, expected = item["question"], item["expected"]
            result = self.query(
                question, document_id=document_id, temperature=0.0, max_tokens=max_tokens
            )
            got = result["answer"]
            if _score_answer(got, expected, strict=strict, threshold=threshold):
                correct += 1
            else:
                misses.append({"question": question, "expected": expected, "got": got})
        n = len(qa)
        accuracy = round(correct / n, 4) if n else 0.0
        if hasattr(self._db, "set_reliability"):  # no-op here; lights up with the deferred column
            self._db.set_reliability(document_id, accuracy)
        return {
            "document": {
                "id": doc["id"], "file_name": doc["file_name"], "n_tokens": doc["n_tokens"],
            },
            "n": n, "correct": correct, "accuracy": accuracy, "strict": strict, "misses": misses,
        }

    def _make_hot(self, doc: dict) -> tuple[int, str]:
        """Ensure doc's KV state is resident in a slot.

        Returns (slot_id, source) where source is "memory" (already hot),
        "disk" (restored from its cache file), or "recomputed" (the completion
        itself must re-evaluate the prefix)."""
        self._ensure_model_marker()
        slot, already_hot = self._slot_for(doc["id"])
        with self._slots_guard:
            self._slot_used[slot] = time.monotonic()
        if already_hot:
            return slot, "memory"

        with self._slots_guard:
            evicted = self._slots.get(slot)
            self._slots[slot] = doc["id"]
        if evicted is not None:
            logger.info("Evicting document %s from slot %s (LRU)", evicted, slot)

        cache_file = doc.get("cache_file")
        if cache_file and (self._settings.cache_dir / cache_file).exists():
            try:
                restored = self._llama.slot_restore(cache_file, slot)
                logger.info(
                    "Restored %s tokens for document %s into slot %s from %s",
                    restored.get("n_restored", "?"), doc["id"], slot, cache_file,
                )
                return slot, "disk"
            except LlamaError as exc:
                logger.warning("Restore of %s failed, recomputing: %s", cache_file, exc)
        else:
            logger.warning(
                "Cache file %s for document %s missing, recomputing", cache_file, doc["id"]
            )
        return slot, "recomputed"

    def _schedule_resave(self, doc: dict, slot: int) -> None:
        """Run the self-heal re-save via self._spawn so the healed query's
        response doesn't also pay for slot_save.

        The deferred body acquires the big lock, re-checks under the slots
        guard that the slot still holds this document (another query may have
        evicted it meanwhile — then the NEXT heal retries), and only then
        saves. Nothing raised inside it ever propagates."""

        def deferred() -> None:
            try:
                with self._lock:
                    with self._slots_guard:
                        current = self._slots.get(slot)
                    if current != doc["id"]:
                        logger.info(
                            "Deferred re-save skipped: slot %s now holds %s, not %s",
                            slot, current, doc["id"],
                        )
                        return
                    self._resave(doc, slot)
            except Exception:  # deferred work must never propagate
                logger.exception("Deferred re-save for document %s failed", doc["id"])

        self._spawn(deferred)

    def _resave(self, doc: dict, slot: int) -> None:
        """Self-heal: persist the freshly recomputed KV state for next time."""
        try:
            n_tokens = doc.get("n_tokens") or self._llama.count_tokens(doc["content"])
            cache_file = self._cache_filename(doc["id"])
            self._llama.slot_save(cache_file, slot)
            if not self._db.mark_cached(doc["id"], n_tokens, cache_file):
                # The document was deleted while we were recomputing. Don't
                # strand a cache file no DB row points at — undo the save.
                (self._settings.cache_dir / cache_file).unlink(missing_ok=True)
                logger.info(
                    "Document %s vanished during self-heal; removed its re-saved cache file",
                    doc["id"],
                )
                return
            logger.info("Re-saved cache for document %s from slot %s", doc["id"], slot)
        except LlamaError as exc:
            logger.warning("Could not re-save cache for document %s: %s", doc["id"], exc)

    # --- management ----------------------------------------------------------

    def list_documents(self) -> list[dict]:
        return self._db.list_documents()

    def delete_document(self, document_id: int) -> bool:
        deleted = self._db.delete_document(document_id)
        if deleted is None:
            return False
        if deleted.get("cache_file"):
            path = self._settings.cache_dir / deleted["cache_file"]
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("Could not delete cache file %s: %s", path, exc)
        with self._lock:
            with self._slots_guard:
                doomed = [s for s, hot_id in self._slots.items() if hot_id == document_id]
            for slot in doomed:
                try:
                    self._llama.slot_erase(slot)  # I/O — the guard is not held here
                except LlamaError:
                    pass
                with self._slots_guard:
                    self._slots.pop(slot, None)
                    self._slot_used.pop(slot, None)
        return True

    def maintenance(self) -> dict:
        """Reconcile disk (cache files) with the database."""
        known = self._db.all_cache_files()
        on_disk = {p.name: p for p in self._settings.cache_dir.glob("*.bin")}

        now = time.time()
        orphans_removed, orphans_failed, skipped_recent = [], [], []
        for name, path in sorted(on_disk.items()):
            if name in known:
                continue
            try:
                age_s = now - path.stat().st_mtime
            except OSError:
                continue  # vanished mid-scan — nothing left to clean
            if age_s < self._settings.maintenance_grace_s:
                # Too young to be a confirmed orphan: an ingest or self-heal
                # may still be in flight and its DB row not visible yet.
                # Report it, don't delete it.
                skipped_recent.append(name)
                continue
            try:
                path.unlink()
                orphans_removed.append(name)
            except OSError as exc:
                orphans_failed.append({"file": name, "error": str(exc)})

        missing = sorted(known - set(on_disk))  # will self-heal on next query
        cache_bytes = 0
        for p in on_disk.values():
            try:
                cache_bytes += p.stat().st_size
            except OSError:
                # A concurrent delete_document may unlink a cache file between the
                # glob above and this stat; a missing file just contributes 0 bytes.
                pass
        return {
            "orphan_files_removed": orphans_removed,
            "orphan_files_failed": orphans_failed,
            "skipped_recent": skipped_recent,
            "missing_cache_files": missing,
            "cache_files": len(on_disk) - len(orphans_removed),
            "cache_bytes": cache_bytes,
            **self._db.stats(),
        }

    def usage_stats(self) -> dict:
        """GET /stats: usage aggregates + a cost-savings estimate. Pure read of
        query_log; touches neither lock, so it answers even mid-generation and
        when inference is down. Pricing policy lives here (with Settings), not in
        the DB layer, mirroring how list_documents/maintenance delegate."""
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
                "note": (
                    "Estimate: tokens_reused / 1000 * cloud_price_per_1k_input. "
                    "Set CLOUD_PRICE_PER_1K_INPUT to your provider's input price to enable."
                ),
            },
        }

    def health(self) -> dict:
        # Snapshot the slot map under the micro-guard, NOT the big lock: a
        # long generation can hold _lock for minutes, and health must answer
        # promptly regardless. The guard alone is enough — every slot-map
        # mutation happens inside it, so the snapshot can never observe a
        # mid-mutation dict ("dictionary changed size during iteration").
        with self._slots_guard:
            hot = dict(sorted(self._slots.items()))
        report: dict = {
            "status": "ok",
            # slot_id -> document_id whose KV state is resident in RAM
            "hot_documents": {str(slot): doc for slot, doc in hot.items()},
            "slots": max(self._settings.cag_slots, 1),
        }
        try:
            report["llama_server"] = self._llama.health()
        except LlamaError as exc:
            report["status"] = "degraded"
            report["llama_server"] = {"error": str(exc)}
        try:
            self._db.ping()
            report["database"] = "ok"
        except Exception as exc:  # psycopg errors vary; any failure = degraded
            report["status"] = "degraded"
            report["database"] = {"error": str(exc)}
        return report
