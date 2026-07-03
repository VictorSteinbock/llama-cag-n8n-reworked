import json

import pytest

from app.cag import (
    DEFAULT_VERDICT_SCHEMA,
    CagEngine,
    DocumentTooLargeError,
    NoCachedDocumentError,
    UnknownDocumentError,
)

DOC = "The capital of Freedonia is Fredville. " * 20


def test_ingest_warms_and_persists(engine, fake_llama, fake_db, tmp_path):
    result = engine.ingest_text("facts.txt", DOC)

    assert result["status"] == "cached"
    assert result["n_tokens"] == 50
    assert result["deduplicated"] is False
    assert result["cache_file"] == "doc-1.bin"
    assert (tmp_path / "doc-1.bin").exists()
    # erase -> warm chat -> save, in that order
    names = [c[0] for c in fake_llama.calls if c[0].startswith(("slot_", "chat"))]
    assert names == ["slot_erase", "chat", "slot_save"]


def test_ingest_deduplicates_by_content(engine, fake_llama):
    engine.ingest_text("facts.txt", DOC)
    again = engine.ingest_text("renamed-copy.txt", DOC)

    assert again["deduplicated"] is True
    assert again["id"] == 1
    assert len(fake_llama.called("chat")) == 1  # no second warm


def test_concurrent_duplicate_ingest_becomes_dedupe(engine, fake_llama, fake_db):
    # Two requests ingest identical content at once: the loser's INSERT hits
    # the content_sha256 UNIQUE constraint (insert_document returns None) and
    # must come back as a dedupe of the winner's row — not a 500.
    fake_db.conflict_on_insert = True

    result = engine.ingest_text("facts.txt", DOC)

    assert result["deduplicated"] is True
    assert len(fake_db.documents) == 1  # exactly the winner's row
    assert fake_llama.called("chat") == []  # the loser never warms


def test_duplicate_ingest_race_with_vanished_winner_raises(engine, fake_db):
    # Pathological: we lost the insert race AND the winner's row is already
    # gone by the re-fetch. That deserves a clear error, not a KeyError.
    fake_db.conflict_on_insert = True
    fake_db.find_by_sha256 = lambda sha256: None

    with pytest.raises(RuntimeError, match="[Cc]oncurrent ingest"):
        engine.ingest_text("facts.txt", DOC)


def test_ingest_rejects_documents_larger_than_context(engine, fake_llama, fake_db):
    fake_llama.tokens_per_text = 950  # > 1000 − 100 answer reserve − 96 prompt overhead

    with pytest.raises(DocumentTooLargeError) as exc:
        engine.ingest_text("big.txt", DOC)

    assert exc.value.limit == 804
    assert fake_db.documents == {}  # rejected before any row was written


def test_query_uses_hot_document_without_restore(engine, fake_llama):
    engine.ingest_text("facts.txt", DOC)
    result = engine.query("What is the capital?")

    assert result["answer"] == "the answer"
    assert result["timings"]["cache_source"] == "memory"
    assert fake_llama.called("slot_restore") == []


def test_query_restores_from_disk_after_restart(fake_llama, fake_db, settings):
    # First engine ingests; second engine simulates an API restart (cold state).
    CagEngine(fake_llama, fake_db, settings).ingest_text("facts.txt", DOC)
    engine2 = CagEngine(fake_llama, fake_db, settings)

    result = engine2.query("What is the capital?")
    assert result["timings"]["cache_source"] == "disk"
    assert len(fake_llama.called("slot_restore")) == 1

    # Second query on the now-hot document: no second restore.
    result2 = engine2.query("And again?")
    assert result2["timings"]["cache_source"] == "memory"
    assert len(fake_llama.called("slot_restore")) == 1


def test_query_self_heals_missing_cache_file(fake_llama, fake_db, settings, tmp_path):
    CagEngine(fake_llama, fake_db, settings).ingest_text("facts.txt", DOC)
    (tmp_path / "doc-1.bin").unlink()  # cache file lost
    engine2 = CagEngine(fake_llama, fake_db, settings)
    engine2._spawn = lambda fn: fn()  # run the deferred re-save inline

    result = engine2.query("What is the capital?")

    assert result["answer"] == "the answer"
    assert result["timings"]["cache_source"] == "recomputed"
    assert (tmp_path / "doc-1.bin").exists()  # re-saved for next time
    assert fake_llama.called("slot_restore") == []  # nothing to restore from


def test_query_targets_specific_document(engine, fake_llama, fake_db):
    engine.ingest_text("first.txt", DOC)
    engine.ingest_text("second.txt", DOC + " More facts.")

    result = engine.query("Q?", document_id=1)
    assert result["document"]["id"] == 1
    # doc 2 was hot after its ingest, so doc 1 had to be restored
    assert len(fake_llama.called("slot_restore")) == 1


def test_query_without_documents_raises(engine):
    with pytest.raises(NoCachedDocumentError):
        engine.query("Anyone home?")


def test_query_threads_history_between_document_and_question(engine, fake_llama):
    engine.ingest_text("facts.txt", DOC)
    history = [
        {"role": "user", "content": "What is the capital?"},
        {"role": "assistant", "content": "Fredville."},
    ]

    engine.query("And its population?", history=history)

    roles = [m["role"] for m in fake_llama.last_messages]
    assert roles == ["system", "user", "assistant", "user"]
    assert fake_llama.last_messages[1]["content"] == "What is the capital?"
    assert fake_llama.last_messages[-1]["content"] == "And its population?"
    # The cached document prefix stays byte-identical regardless of history.
    assert "<document" in fake_llama.last_messages[0]["content"]


def test_query_trims_oldest_history_first(engine, fake_llama):
    # Budget: slot 1000 − doc 50 − answer reserve 100 − prompt overhead 96 = 754.
    # Three turns of est. 308 tokens each (900 chars // 3 + 8) plus the question
    # (~9) exceed it; dropping the single oldest turn brings it back under.
    engine.ingest_text("facts.txt", DOC)
    history = [
        {"role": "user", "content": "a" * 900},
        {"role": "assistant", "content": "b" * 900},
        {"role": "user", "content": "c" * 900},
    ]

    result = engine.query("And?", history=history)

    assert result["timings"]["history_trimmed"] == 1
    contents = [m["content"] for m in fake_llama.last_messages]
    assert "a" * 900 not in contents  # oldest dropped
    assert "b" * 900 in contents and "c" * 900 in contents  # newer turns kept
    assert contents[-1] == "And?"  # the question is always last and untouched


def test_query_history_trimming_never_drops_question(engine, fake_llama):
    engine.ingest_text("facts.txt", DOC)
    # A single turn far over the whole budget: the turn goes, the question stays.
    history = [{"role": "user", "content": "x" * 6000}]

    result = engine.query("Still here?", history=history)

    assert result["timings"]["history_trimmed"] == 1
    roles = [m["role"] for m in fake_llama.last_messages]
    assert roles == ["system", "user"]
    assert fake_llama.last_messages[-1]["content"] == "Still here?"


def test_query_short_history_is_not_trimmed(engine, fake_llama):
    engine.ingest_text("facts.txt", DOC)
    history = [
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Hello"},
    ]

    result = engine.query("And?", history=history)

    # Convention: the key is present only when trimming actually occurred.
    assert "history_trimmed" not in result["timings"]
    assert len(fake_llama.last_messages) == 4  # system + both turns + question


def test_query_forwards_json_schema_without_touching_prompt(engine, fake_llama):
    engine.ingest_text("facts.txt", DOC)
    schema = {
        "type": "object",
        "properties": {"verdict": {"type": "string"}},
        "required": ["verdict"],
    }

    # Capture the exact system prefix a plain (schema-less) query produces...
    engine.query("What is the capital?")
    plain_system = fake_llama.last_messages[0]["content"]
    assert fake_llama.last_json_schema is None

    # ...then the same query with a schema: the schema reaches chat() verbatim
    # and the system prefix is byte-identical (schema affects sampling only).
    engine.query("What is the capital?", json_schema=schema)
    assert fake_llama.last_json_schema == schema
    assert fake_llama.last_messages[0]["content"] == plain_system


def test_query_failure_is_logged(engine, fake_llama, fake_db):
    engine.ingest_text("facts.txt", DOC)

    def boom(*a, **k):
        from app.llama import LlamaError
        raise LlamaError("inference exploded")

    fake_llama.chat = boom
    with pytest.raises(Exception, match="inference exploded"):
        engine.query("Q?")

    failed = [q for q in fake_db.queries if not q["success"]]
    assert len(failed) == 1
    assert "exploded" in failed[0]["error"]


def test_delete_removes_cache_file_and_slot(engine, fake_llama, fake_db, tmp_path):
    engine.ingest_text("facts.txt", DOC)
    assert (tmp_path / "doc-1.bin").exists()

    assert engine.delete_document(1) is True
    assert not (tmp_path / "doc-1.bin").exists()
    assert fake_db.documents == {}
    assert len(fake_llama.called("slot_erase")) == 2  # warm-time + delete-time

    assert engine.delete_document(99) is False


def test_maintenance_removes_orphans_and_reports_missing(engine, fake_db, tmp_path):
    import os
    import time

    engine.ingest_text("facts.txt", DOC)
    (tmp_path / "orphan.bin").write_bytes(b"junk")
    fake_db.documents[1]["cache_file"] = "gone.bin"  # db points at a lost file
    # Age both stray files past the grace window so they are confirmed orphans.
    backdated = time.time() - 7200
    for name in ("doc-1.bin", "orphan.bin"):
        os.utime(tmp_path / name, (backdated, backdated))

    report = engine.maintenance()

    assert report["orphan_files_removed"] == ["doc-1.bin", "orphan.bin"]
    assert report["missing_cache_files"] == ["gone.bin"]
    assert report["skipped_recent"] == []
    assert not (tmp_path / "orphan.bin").exists()


def test_maintenance_grace_window_spares_recent_orphans(engine, fake_db, tmp_path):
    import os
    import time

    engine.ingest_text("facts.txt", DOC)  # doc-1.bin is known to the DB — untouched
    fresh = tmp_path / "fresh-orphan.bin"
    fresh.write_bytes(b"junk")  # could be an ingest still in flight
    old = tmp_path / "old-orphan.bin"
    old.write_bytes(b"junk")
    backdated = time.time() - 7200  # well past the 3600 s default grace
    os.utime(old, (backdated, backdated))

    report = engine.maintenance()

    assert report["orphan_files_removed"] == ["old-orphan.bin"]
    assert report["skipped_recent"] == ["fresh-orphan.bin"]
    assert fresh.exists()  # spared
    assert not old.exists()  # confirmed orphan, removed


def test_two_slots_keep_two_documents_hot(fake_llama, fake_db, tmp_path):
    from app.config import Settings

    settings = Settings(
        cache_dir=tmp_path, llama_ctx_size=1000, answer_reserve_tokens=100,
        cag_slots=2, db_password="test",
    )
    engine = CagEngine(fake_llama, fake_db, settings)
    engine.ingest_text("first.txt", DOC)
    engine.ingest_text("second.txt", DOC + " More facts.")

    # Warms landed in distinct slots.
    assert {slot for _, _, slot in fake_llama.called("slot_save")} == {0, 1}

    # Alternating between both docs never touches disk.
    for doc_id in (1, 2, 1, 2):
        result = engine.query("Q?", document_id=doc_id)
        assert result["timings"]["cache_source"] == "memory"
    assert fake_llama.called("slot_restore") == []


def test_lru_eviction_when_all_slots_busy(fake_llama, fake_db, tmp_path):
    from app.config import Settings

    settings = Settings(
        cache_dir=tmp_path, llama_ctx_size=1000, answer_reserve_tokens=100,
        cag_slots=2, db_password="test",
    )
    engine = CagEngine(fake_llama, fake_db, settings)
    engine.ingest_text("first.txt", DOC)
    engine.ingest_text("second.txt", DOC + " b")
    engine.ingest_text("third.txt", DOC + " c")  # evicts doc 1 (LRU)

    result = engine.query("Q?", document_id=1)  # must come back from disk
    assert result["timings"]["cache_source"] == "disk"
    # ...evicting doc 2, the least recently used of (2, 3).
    assert engine.query("Q?", document_id=3)["timings"]["cache_source"] == "memory"
    assert engine.query("Q?", document_id=2)["timings"]["cache_source"] == "disk"


def test_token_limit_is_per_slot(fake_llama, fake_db, tmp_path):
    from app.config import Settings

    settings = Settings(
        cache_dir=tmp_path, llama_ctx_size=1000, answer_reserve_tokens=100,
        cag_slots=2, db_password="test",
    )
    engine = CagEngine(fake_llama, fake_db, settings)
    # 450 fits the single-slot limit (1000−100−96 = 804) but not the two-slot
    # one (500−100−96 = 304).
    fake_llama.tokens_per_text = 450

    with pytest.raises(DocumentTooLargeError) as exc:
        engine.ingest_text("big.txt", DOC)
    assert exc.value.limit == 304
    assert "CAG_SLOTS=2" in str(exc.value)


def test_health_reports_degraded_dependencies(engine, fake_llama, fake_db):
    assert engine.health()["status"] == "ok"

    fake_llama.healthy = False
    report = engine.health()
    assert report["status"] == "degraded"
    assert "error" in report["llama_server"]

    fake_llama.healthy = True
    fake_db.ping_ok = False
    assert engine.health()["status"] == "degraded"


def test_health_reports_hot_documents(engine, fake_llama):
    engine.ingest_text("facts.txt", DOC)
    report = engine.health()
    # slot 0 holds document 1 after warming
    assert report["hot_documents"] == {"0": 1}
    assert report["slots"] == 1


def test_health_returns_promptly_while_generation_holds_lock(engine):
    # Two-lock discipline: health() snapshots the slot map under the momentary
    # _slots_guard, NOT the big _lock — so a long generation (which holds _lock
    # for minutes) must not make health() queue behind it.
    import threading
    import time

    locked = threading.Event()
    release = threading.Event()

    def long_generation():
        with engine._lock:
            locked.set()
            release.wait(timeout=10.0)

    holder = threading.Thread(target=long_generation)
    holder.start()
    assert locked.wait(timeout=2.0)  # the "generation" now owns the big lock

    started = time.monotonic()
    report = engine.health()
    elapsed = time.monotonic() - started

    release.set()
    holder.join(timeout=2.0)
    assert report["status"] == "ok"
    assert elapsed < 2.0  # answered while the lock was held — never queued


def test_health_never_raises_during_concurrent_slot_churn(fake_llama, fake_db, tmp_path):
    # Stress the exact race the slots guard closes: one thread churns the slot
    # map while another hammers health(). Under a lock-free read this raised
    # RuntimeError almost immediately; with the guarded snapshot it never does.
    import threading

    from app.config import Settings

    settings = Settings(
        cache_dir=tmp_path, llama_ctx_size=100000, answer_reserve_tokens=100,
        cag_slots=4, db_password="test",
    )
    engine = CagEngine(fake_llama, fake_db, settings)
    stop = threading.Event()
    errors: list[Exception] = []

    def churn():
        i = 0
        while not stop.is_set():
            # Mirror the engine's writers: map mutations happen under the
            # micro-guard, nested inside the big lock.
            with engine._lock:
                with engine._slots_guard:
                    engine._slots[i % 4] = i
                    engine._slot_used[i % 4] = float(i)
                    if (i % 8) >= 4:
                        engine._slots.pop(i % 4, None)
            i += 1

    def poll():
        try:
            for _ in range(3000):
                engine.health()
        except Exception as exc:  # noqa: BLE001 - the whole point is to catch it
            errors.append(exc)
        finally:
            stop.set()

    writer = threading.Thread(target=churn)
    reader = threading.Thread(target=poll)
    writer.start()
    reader.start()
    reader.join(timeout=10.0)
    stop.set()
    writer.join(timeout=2.0)
    assert errors == []


def test_delete_during_concurrent_query_keeps_slot_map_consistent(
    engine, fake_llama, fake_db, tmp_path
):
    # The delete slot-erase path and a query both mutate the slot map under the
    # engine lock. Fire a delete of the same document from inside the chat call
    # (which the query runs while holding the lock); the delete's own lock
    # acquisition must wait, then erase cleanly, leaving no dangling slot entry
    # and no crash — and the in-flight query still returns its answer.
    import threading

    engine.ingest_text("facts.txt", DOC)  # doc 1 hot in slot 0
    state: dict = {}
    real_chat = fake_llama.chat

    def chat_then_delete(*args, **kwargs):
        deleter = threading.Thread(target=lambda: state.update(ok=engine.delete_document(1)))
        deleter.start()
        deleter.join(timeout=0.2)  # blocked on the lock this query still holds
        state["blocked_during_query"] = deleter.is_alive()
        state["deleter"] = deleter
        return real_chat(*args, **kwargs)

    fake_llama.chat = chat_then_delete
    out = engine.query("What is the capital?", document_id=1)
    state["deleter"].join(timeout=2.0)  # completes once the query released the lock

    assert out["answer"] == "the answer"  # the query still succeeded
    assert state["blocked_during_query"] is True  # delete waited on the lock
    assert state["ok"] is True
    assert engine._slots == {}  # slot erased, no dangling entry
    assert fake_db.documents == {}  # document row gone
    # And no cache file for the deleted document is left behind on disk.
    assert not (tmp_path / "doc-1.bin").exists()


def test_resave_after_concurrent_delete_strands_no_file(fake_llama, fake_db, settings, tmp_path):
    # Self-heal path: the cache file is missing, so the query recomputes and
    # re-saves. If the document row vanished mid-recompute (delete_document
    # removes the DB row before taking the engine lock), mark_cached updates
    # zero rows — the freshly saved file must be rolled back, not stranded.
    CagEngine(fake_llama, fake_db, settings).ingest_text("facts.txt", DOC)
    (tmp_path / "doc-1.bin").unlink()  # force the recompute path
    engine2 = CagEngine(fake_llama, fake_db, settings)
    engine2._spawn = lambda fn: fn()  # run the deferred re-save inline

    real_chat = fake_llama.chat

    def chat_and_lose_row(*args, **kwargs):
        fake_db.documents.pop(1)  # the concurrent delete wins the DB row mid-chat
        return real_chat(*args, **kwargs)

    fake_llama.chat = chat_and_lose_row
    result = engine2.query("Q?", document_id=1)

    assert result["timings"]["cache_source"] == "recomputed"
    assert len(fake_llama.called("slot_save")) == 2  # warm-time + the re-save
    assert not (tmp_path / "doc-1.bin").exists()  # ...which rolled itself back


def test_deferred_resave_skips_when_slot_reassigned(fake_llama, fake_db, settings, tmp_path):
    # The healed query schedules its re-save; before the deferred job runs,
    # the slot gets reassigned to another document. The job must notice and
    # skip — saving now would persist the WRONG document's KV under doc-1.bin.
    CagEngine(fake_llama, fake_db, settings).ingest_text("facts.txt", DOC)
    (tmp_path / "doc-1.bin").unlink()  # force the recompute path
    engine2 = CagEngine(fake_llama, fake_db, settings)
    deferred_jobs = []
    engine2._spawn = deferred_jobs.append  # capture instead of running

    result = engine2.query("Q?", document_id=1)
    assert result["timings"]["cache_source"] == "recomputed"
    assert len(deferred_jobs) == 1

    with engine2._slots_guard:
        engine2._slots[0] = 999  # another document became hot meanwhile

    saves_before = len(fake_llama.called("slot_save"))
    deferred_jobs[0]()  # now run the deferred re-save

    assert len(fake_llama.called("slot_save")) == saves_before  # skipped
    assert not (tmp_path / "doc-1.bin").exists()  # nothing was written


def test_deferred_resave_exception_does_not_propagate(fake_llama, fake_db, settings, tmp_path):
    CagEngine(fake_llama, fake_db, settings).ingest_text("facts.txt", DOC)
    (tmp_path / "doc-1.bin").unlink()
    engine2 = CagEngine(fake_llama, fake_db, settings)
    engine2._spawn = lambda fn: fn()  # inline: any leak would fail the query

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    fake_llama.slot_save = boom
    result = engine2.query("Q?", document_id=1)  # must not raise

    assert result["answer"] == "the answer"
    assert result["timings"]["cache_source"] == "recomputed"


def test_model_switch_invalidates_stale_caches_once(fake_llama, fake_db, settings, tmp_path):
    # llama.cpp restores state files from a same-geometry model silently (the
    # file carries no weight identity), so the engine fingerprints the model
    # via /props model_path: on mismatch every *.bin is wiped once (they
    # self-heal) and the marker is rewritten.
    CagEngine(fake_llama, fake_db, settings).ingest_text("facts.txt", DOC)
    marker = tmp_path / "model.marker"
    assert marker.read_text(encoding="utf-8") == "/models/fake-model.gguf"

    fake_llama.model_path = "/models/other-model.gguf"  # the switch
    stray = tmp_path / "doc-99.bin"
    stray.write_bytes(b"stale kv from the old model")
    engine2 = CagEngine(fake_llama, fake_db, settings)  # new process
    engine2._spawn = lambda fn: fn()

    props_before = len(fake_llama.called("props"))  # engine1 checked once itself
    result = engine2.query("Q?", document_id=1)

    assert result["timings"]["cache_source"] == "recomputed"  # old bin was wiped
    assert not stray.exists()  # all stale bins removed, not just this doc's
    assert marker.read_text(encoding="utf-8") == "/models/other-model.gguf"
    assert (tmp_path / "doc-1.bin").exists()  # healed under the NEW model
    assert len(fake_llama.called("props")) == props_before + 1

    engine2.query("Again?", document_id=1)  # second interaction: no re-check
    assert len(fake_llama.called("props")) == props_before + 1


def test_matching_model_marker_leaves_caches_untouched(fake_llama, fake_db, settings, tmp_path):
    CagEngine(fake_llama, fake_db, settings).ingest_text("facts.txt", DOC)
    engine2 = CagEngine(fake_llama, fake_db, settings)  # same model, new process

    result = engine2.query("Q?")

    assert result["timings"]["cache_source"] == "disk"  # bin survived the check
    assert (tmp_path / "doc-1.bin").exists()


def test_model_marker_check_defers_while_llama_down(fake_llama, fake_db, settings, tmp_path):
    # /props unreachable: the check must defer (no crash, nothing deleted,
    # marker untouched) and run on the next interaction once llama is back.
    CagEngine(fake_llama, fake_db, settings).ingest_text("facts.txt", DOC)
    marker = tmp_path / "model.marker"
    marker.write_text("/models/OLD.gguf", encoding="utf-8")  # stale marker
    engine2 = CagEngine(fake_llama, fake_db, settings)

    fake_llama.healthy = False  # props raises; chat/restore still scripted OK
    result = engine2.query("Q?")
    assert result["timings"]["cache_source"] == "disk"  # query unaffected
    assert marker.read_text(encoding="utf-8") == "/models/OLD.gguf"  # deferred
    assert (tmp_path / "doc-1.bin").exists()  # nothing deleted blindly

    fake_llama.healthy = True  # llama back: next interaction reconciles
    engine2.query("Q?")
    assert marker.read_text(encoding="utf-8") == "/models/fake-model.gguf"
    assert not (tmp_path / "doc-1.bin").exists()  # stale bins wiped once


def test_maintenance_tolerates_cache_file_vanishing_mid_scan(engine, fake_db, tmp_path):
    # A concurrent delete can unlink a .bin between maintenance's glob and its
    # stat(); that must not 500 the maintenance endpoint.
    import pathlib
    from unittest.mock import patch

    engine.ingest_text("facts.txt", DOC)  # writes doc-1.bin, recorded in the DB

    real_stat = pathlib.Path.stat
    victim = tmp_path / "doc-1.bin"
    assert victim.exists()  # ingest wrote it
    vanished = {"done": False}

    def stat_but_vanish(self, *args, **kwargs):
        # Unlink the victim the first time maintenance stats it, then let the
        # real stat raise FileNotFoundError — the exact concurrent-delete TOCTOU.
        # Avoid Path.exists() here: on 3.13 it routes back through stat() and
        # would recurse into this patch.
        if self == victim and not vanished["done"]:
            vanished["done"] = True
            victim.unlink()
        return real_stat(self, *args, **kwargs)

    with patch.object(pathlib.Path, "stat", stat_but_vanish):
        report = engine.maintenance()  # must not raise

    assert "cache_bytes" in report
    assert report["cached_documents"] == 1
    assert vanished["done"]  # the vanish actually happened during the scan


# --- F1/F3: verify_claim (quote-grounding + conditions) --------------------

def _verdict(verdict, quote, conditions="", claim="the claim"):
    return json.dumps(
        {"claim": claim, "verdict": verdict, "quote": quote, "conditions": conditions}
    )


def test_verify_grounded_supported(engine, fake_llama):
    engine.ingest_text("facts.txt", DOC)
    fake_llama.answer_json = _verdict("supported", "The capital of Freedonia is Fredville")

    result = engine.verify_claim("Fredville is the capital")

    assert result["verdict"] == "supported"
    assert result["quote_grounded"] is True
    assert result["grounding_method"] == "exact"
    assert result["match_ratio"] == 1.0
    assert result["conditions"] == ""
    assert result["document"]["id"] == 1


def test_verify_catches_fabricated_quote(engine, fake_llama):
    engine.ingest_text("facts.txt", DOC)
    fake_llama.answer_json = _verdict(
        "supported", "The capital of Freedonia is Metropolis by the sea"
    )

    result = engine.verify_claim("Metropolis is the capital")

    assert result["verdict"] == "supported"  # the model claimed support...
    assert result["quote_grounded"] is False  # ...but the quote isn't in the doc
    assert result["match_ratio"] < 0.9


def test_verify_absent_leaves_grounding_none(engine, fake_llama):
    engine.ingest_text("facts.txt", DOC)
    fake_llama.answer_json = _verdict("absent", "")

    result = engine.verify_claim("The document mentions dragons")

    assert result["verdict"] == "absent"
    assert result["quote_grounded"] is None
    assert result["grounding_method"] == "absent"


def test_verify_surfaces_conditions(engine, fake_llama):
    engine.ingest_text("facts.txt", DOC)
    fake_llama.answer_json = _verdict(
        "contradicted", "The capital of Freedonia is Fredville",
        conditions="only if the item is defective",
    )

    result = engine.verify_claim("Refunds are always available")

    assert result["conditions"] == "only if the item is defective"


def test_verify_non_json_answer_yields_error_verdict(engine, fake_llama):
    engine.ingest_text("facts.txt", DOC)
    fake_llama.answer_json = "sorry, I can't do that"

    result = engine.verify_claim("anything")

    assert result["verdict"] == "error"
    assert result["quote_grounded"] is None
    assert result["match_ratio"] == 0.0


def test_verify_reuses_query_prefix_byte_identical(engine, fake_llama):
    engine.ingest_text("facts.txt", DOC)
    engine.query("hi")  # a plain, schema-less query over the same document
    baseline_system = fake_llama.last_messages[0]["content"]

    fake_llama.answer_json = _verdict("supported", "The capital of Freedonia is Fredville")
    engine.verify_claim("Fredville is the capital")

    # Same document -> byte-identical system prefix (invariant 1). The schema
    # rides in sampling; the instruction rides in the last user turn.
    assert fake_llama.last_messages[0]["content"] == baseline_system
    assert fake_llama.last_json_schema == DEFAULT_VERDICT_SCHEMA
    assert fake_llama.last_messages[-1]["content"].startswith("Verify this claim strictly")


def test_verify_unknown_document_raises(engine, fake_llama):
    engine.ingest_text("facts.txt", DOC)
    with pytest.raises(UnknownDocumentError):
        engine.verify_claim("x", document_id=999)


# --- F5: engine usage_stats wrapper applies pricing ------------------------

def test_engine_usage_stats_applies_price(fake_llama, fake_db, tmp_path):
    from app.config import Settings

    settings = Settings(
        cache_dir=tmp_path, llama_ctx_size=1000, answer_reserve_tokens=100,
        db_password="test", cloud_price_per_1k_input=0.002,
    )
    engine = CagEngine(fake_llama, fake_db, settings)
    engine.ingest_text("facts.txt", DOC)
    engine.query("q?")

    stats = engine.usage_stats()

    # Pricing lives in the engine wrapper, not the DB fake: it wraps
    # Database.usage_stats() and multiplies all-time reused tokens by the price.
    reused = stats["windows"]["all"]["tokens_reused"]
    assert stats["savings"]["estimated_usd"] == round(reused / 1000 * 0.002, 4)
    assert stats["savings"]["cloud_price_per_1k_input"] == 0.002
