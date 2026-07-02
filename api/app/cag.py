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
import logging
import re
import threading
import time

from .config import Settings
from .db import Database
from .extract import extract_text
from .llama import LlamaClient, LlamaError

logger = logging.getLogger(__name__)

SYSTEM_TEMPLATE = (
    "You are a precise assistant. Answer questions using only the information in the "
    "document below. If the document does not contain the answer, say so plainly.\n\n"
    '<document name="{name}">\n{content}\n</document>'
)

WARM_USER_MESSAGE = "Reply with the single word: ready"


class DocumentTooLargeError(Exception):
    def __init__(self, n_tokens: int, limit: int, ctx_size: int, slots: int) -> None:
        self.n_tokens = n_tokens
        self.limit = limit
        self.ctx_size = ctx_size
        self.slots = slots
        per_slot = f" ÷ CAG_SLOTS={slots}" if slots > 1 else ""
        super().__init__(
            f"Document is {n_tokens} tokens but the per-slot limit is {limit} "
            f"(LLAMA_CTX_SIZE={ctx_size}{per_slot}, minus answer head-room). "
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


class CagEngine:
    def __init__(self, llama: LlamaClient, db: Database, settings: Settings) -> None:
        self._llama = llama
        self._db = db
        self._settings = settings
        # Serializes slot use: restore + completion must be atomic, and CPU
        # inference is sequential anyway. Concurrent requests queue here.
        self._lock = threading.Lock()
        self._slots: dict[int, int] = {}  # slot_id -> document_id currently hot
        self._slot_used: dict[int, float] = {}  # slot_id -> monotonic last use

    # --- helpers -------------------------------------------------------------

    def _system_message(self, file_name: str, content: str) -> dict:
        return {
            "role": "system",
            "content": SYSTEM_TEMPLATE.format(name=file_name, content=content),
        }

    @staticmethod
    def _cache_filename(document_id: int) -> str:
        return f"doc-{document_id}.bin"

    def _token_limit(self) -> int:
        return self._settings.slot_ctx_size - self._settings.answer_reserve_tokens

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

        try:
            with self._lock:
                slot, cache_source = self._make_hot(doc)
                healed = cache_source == "recomputed"
                result = self._llama.chat(
                    [
                        self._system_message(doc["file_name"], doc["content"]),
                        {"role": "user", "content": question},
                    ],
                    max_tokens=max_tokens,
                    temperature=temperature,
                    slot_id=slot,
                    warm=healed,  # recomputing a full prefix deserves the long timeout
                )
                if healed:
                    self._resave(doc, slot)
        except LlamaError as exc:
            self._db.log_query(
                document_id=doc["id"], question=question, answer=None,
                success=False, error=str(exc),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            raise

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
        return {
            "answer": result["content"],
            "document": {
                "id": doc["id"],
                "file_name": doc["file_name"],
                "n_tokens": doc["n_tokens"],
            },
            "duration_ms": duration_ms,
            "timings": {
                "prompt_tokens_evaluated": timings.get("prompt_n"),
                "prompt_tokens_from_cache": timings.get("cache_n"),
                "answer_tokens": timings.get("predicted_n"),
                # "memory": doc was already hot; "disk": KV state restored from
                # its cache file; "recomputed": self-heal path, prefix was
                # re-evaluated (and re-saved) because no usable cache existed.
                "cache_source": cache_source,
            },
        }

    def _make_hot(self, doc: dict) -> tuple[int, str]:
        """Ensure doc's KV state is resident in a slot.

        Returns (slot_id, source) where source is "memory" (already hot),
        "disk" (restored from its cache file), or "recomputed" (the completion
        itself must re-evaluate the prefix)."""
        slot, already_hot = self._slot_for(doc["id"])
        self._slot_used[slot] = time.monotonic()
        if already_hot:
            return slot, "memory"

        evicted = self._slots.get(slot)
        if evicted is not None:
            logger.info("Evicting document %s from slot %s (LRU)", evicted, slot)
        self._slots[slot] = doc["id"]

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

    def _resave(self, doc: dict, slot: int) -> None:
        """Self-heal: persist the freshly recomputed KV state for next time."""
        try:
            n_tokens = doc.get("n_tokens") or self._llama.count_tokens(doc["content"])
            self._llama.slot_save(self._cache_filename(doc["id"]), slot)
            self._db.mark_cached(doc["id"], n_tokens, self._cache_filename(doc["id"]))
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
            for slot, hot_id in list(self._slots.items()):
                if hot_id == document_id:
                    try:
                        self._llama.slot_erase(slot)
                    except LlamaError:
                        pass
                    del self._slots[slot]
                    self._slot_used.pop(slot, None)
        return True

    def maintenance(self) -> dict:
        """Reconcile disk (cache files) with the database."""
        known = self._db.all_cache_files()
        on_disk = {p.name: p for p in self._settings.cache_dir.glob("*.bin")}

        orphans_removed, orphans_failed = [], []
        for name, path in sorted(on_disk.items()):
            if name in known:
                continue
            try:
                path.unlink()
                orphans_removed.append(name)
            except OSError as exc:
                orphans_failed.append({"file": name, "error": str(exc)})

        missing = sorted(known - set(on_disk))  # will self-heal on next query
        cache_bytes = sum(p.stat().st_size for p in on_disk.values() if p.exists())
        return {
            "orphan_files_removed": orphans_removed,
            "orphan_files_failed": orphans_failed,
            "missing_cache_files": missing,
            "cache_files": len(on_disk) - len(orphans_removed),
            "cache_bytes": cache_bytes,
            **self._db.stats(),
        }

    def health(self) -> dict:
        report: dict = {
            "status": "ok",
            # slot_id -> document_id whose KV state is resident in RAM
            "hot_documents": {str(slot): doc for slot, doc in sorted(self._slots.items())},
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
