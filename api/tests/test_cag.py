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
    # The restore is ATTEMPTED even though our local view says the file is
    # gone — the server (which may hold the file in native mode) is the
    # authority; its failure is what routes us to the recompute path.
    assert len(fake_llama.called("slot_restore")) == 1


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
    # Budget: slot 1000 − doc 50 − prompt overhead 96 = 854 available; the
    # answer allowance (100) plus three turns of est. 308 tokens each
    # (900 chars // 3 + 8) plus the question (~9) exceed it; dropping the
    # oldest turn brings it under — and its now-orphaned assistant reply goes
    # with it, because an assistant-first history desyncs the chat template
    # from the cached document prefix (pair-safe trimming).
    engine.ingest_text("facts.txt", DOC)
    history = [
        {"role": "user", "content": "a" * 900},
        {"role": "assistant", "content": "b" * 900},
        {"role": "user", "content": "c" * 900},
    ]

    result = engine.query("And?", history=history)

    assert result["timings"]["history_trimmed"] == 2
    contents = [m["content"] for m in fake_llama.last_messages]
    assert "a" * 900 not in contents  # oldest dropped
    assert "b" * 900 not in contents  # orphaned assistant reply dropped too
    assert "c" * 900 in contents  # newest turn kept
    roles = [m["role"] for m in fake_llama.last_messages]
    assert roles == ["system", "user", "user"]  # never assistant-first history
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
    # Two fingerprints on the first query: the per-process check, plus the
    # forced re-check every RESTORE performs (llama-server restarts on its own,
    # so restoring is exactly when a same-geometry switch would turn silent).
    assert len(fake_llama.called("props")) == props_before + 2

    engine2.query("Again?", document_id=1)  # hot path: no restore, no re-check
    assert len(fake_llama.called("props")) == props_before + 2


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
    engine2._spawn = lambda fn: fn()  # deterministic: run the heal inline
    result = engine2.query("Q?")
    assert marker.read_text(encoding="utf-8") == "/models/fake-model.gguf"
    # The stale bin was wiped by the reconciliation, the query recomputed, and
    # the self-heal re-saved a FRESH file under the now-verified model.
    assert result["timings"]["cache_source"] == "recomputed"
    assert (tmp_path / "doc-1.bin").exists()


# --- bulletproof-core regressions (2026-07 five-lens review) -----------------

def test_score_answer_containment_respects_word_boundaries():
    from app.cag import _score_answer
    # "no" must not match inside "know"/"cannot" — a yes/no battery would
    # otherwise score wrong answers as correct and inflate calibration.
    assert not _score_answer("I don't know", "no", strict=False, threshold=0.85)
    assert not _score_answer("cannot answer that", "no", strict=False, threshold=0.85)
    assert _score_answer("No, it is not covered.", "no", strict=False, threshold=0.85)
    assert _score_answer("the peak is 12 A for 10 s", "12 A", strict=False, threshold=0.85)


def test_reingest_of_failed_document_heals_it(engine, fake_llama, fake_db):
    from app.llama import LlamaError as LE
    real_chat = fake_llama.chat

    def failing_chat(*a, **k):
        raise LE("llama hiccup mid-warm")

    fake_llama.chat = failing_chat
    with pytest.raises(LE):
        engine.ingest_text("facts.txt", DOC)
    assert fake_db.documents[1]["status"] == "failed"

    fake_llama.chat = real_chat
    result = engine.ingest_text("facts.txt", DOC)  # same bytes, re-dropped
    assert result["deduplicated"] is True
    assert result["status"] == "cached"  # healed — not returned as a corpse


def test_delete_during_ingest_is_a_404_not_a_500(engine, fake_llama, fake_db):
    real_chat = fake_llama.chat

    def chat_and_delete(*a, **k):
        fake_db.documents.pop(1, None)  # a concurrent DELETE wins mid-warm
        return real_chat(*a, **k)

    fake_llama.chat = chat_and_delete
    with pytest.raises(UnknownDocumentError):
        engine.ingest_text("facts.txt", DOC)
    assert engine._slots == {}  # no stranded mapping to the deleted doc


def test_question_alone_is_budget_checked(engine):
    from app.cag import QuestionTooLargeError
    engine.ingest_text("facts.txt", DOC)
    with pytest.raises(QuestionTooLargeError):
        engine.query("q" * 3000)  # ~1008-token estimate vs 854 available, no history


def test_oversized_max_tokens_is_clamped_and_reported(engine):
    engine.ingest_text("facts.txt", DOC)
    result = engine.query("Q?", max_tokens=5000)  # legal per API, over the headroom
    assert result["timings"]["max_tokens_clamped_from"] == 5000
    assert result["answer"] == "the answer"


def test_geometry_shrink_is_a_413_not_a_502(engine, fake_db):
    engine.ingest_text("facts.txt", DOC)
    fake_db.documents[1]["n_tokens"] = 5000  # ingested under a bigger geometry
    with pytest.raises(DocumentTooLargeError):
        engine.query("Q?", document_id=1)


def test_empty_text_ingest_is_rejected(engine):
    from app.extract import UnsupportedDocumentError as UDE
    with pytest.raises(UDE):
        engine.ingest_text("empty.txt", "   ")


def test_chat_failure_unmaps_the_slot(engine, fake_llama):
    from app.llama import LlamaError as LE
    engine.ingest_text("facts.txt", DOC)

    def boom(*a, **k):
        raise LE("mid-request crash")

    fake_llama.chat = boom
    with pytest.raises(LE):
        engine.query("Q?")
    # No lying "memory" label: the retry must take restore-or-recompute.
    assert engine._slots == {}


def test_wrong_cache_label_is_corrected_and_rehealed(engine, fake_llama):
    engine._spawn = lambda fn: fn()
    engine.ingest_text("facts.txt", DOC)  # hot: the map says "memory"
    real_chat = fake_llama.chat

    def chat_cold(*a, **k):
        out = real_chat(*a, **k)
        # The server reused almost nothing — a restart behind our back.
        out["timings"] = {"prompt_n": 500, "cache_n": 0, "predicted_n": 20}
        return out

    fake_llama.chat = chat_cold
    result = engine.query("Q?")
    assert result["timings"]["cache_source"] == "recomputed"  # truth, not the label
    assert len(fake_llama.called("slot_save")) == 2  # warm + corrective re-save


def test_query_bookkeeping_failure_never_discards_the_answer(engine, fake_db):
    engine.ingest_text("facts.txt", DOC)

    def db_down(**kwargs):
        raise ConnectionError("postgres restarting")

    fake_db.log_query = db_down
    result = engine.query("Q?")  # must not raise
    assert result["answer"] == "the answer"


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


def test_verify_absent_carries_recall_probe(engine, fake_llama):
    engine.ingest_text("facts.txt", DOC)

    # Vocabulary alien to the canon: near-zero overlap corroborates "absent".
    fake_llama.answer_json = _verdict("absent", "")
    clean = engine.verify_claim("The reactor coolant pressure exceeds specification")
    assert clean["verdict"] == "absent"
    assert clean["recall"]["max_overlap"] == 0.0

    # A twisted version of a topic the canon DOES discuss: high overlap says
    # "absent" should not be taken at face value downstream.
    fake_llama.answer_json = _verdict("absent", "")
    topical = engine.verify_claim("The capital of Freedonia is Metropolis")
    assert topical["recall"]["max_overlap"] >= 0.5
    assert topical["recall"]["excerpt"]


def test_verify_non_absent_has_no_recall_field_payload(engine, fake_llama):
    engine.ingest_text("facts.txt", DOC)
    fake_llama.answer_json = _verdict("supported", "The capital of Freedonia is Fredville")

    result = engine.verify_claim("Fredville is the capital")

    assert result["recall"] is None  # probe runs only for "absent"


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


# --- F4: calibrate ---------------------------------------------------------

def test_calibrate_scores_and_lists_misses(engine, fake_llama):
    engine.ingest_text("facts.txt", DOC)
    fake_llama.scripted = {"q1": "Fredville", "q2": "wrong", "q3": "42"}

    result = engine.calibrate(1, [
        {"question": "q1", "expected": "Fredville"},
        {"question": "q2", "expected": "Metropolis"},
        {"question": "q3", "expected": "42"},
    ])

    assert result["n"] == 3
    assert result["correct"] == 2
    assert result["accuracy"] == round(2 / 3, 4)
    assert result["misses"] == [{"question": "q2", "expected": "Metropolis", "got": "wrong"}]
    assert result["document"]["id"] == 1


def test_calibrate_containment_counts_correct(engine, fake_llama):
    engine.ingest_text("facts.txt", DOC)
    fake_llama.scripted = {"limit?": "The peak current limit is 12 A."}

    result = engine.calibrate(1, [{"question": "limit?", "expected": "12 A"}])

    assert result["correct"] == 1
    assert result["misses"] == []


def test_calibrate_strict_requires_exact(engine, fake_llama):
    engine.ingest_text("facts.txt", DOC)
    fake_llama.scripted = {"limit?": "The peak current limit is 12 A."}

    result = engine.calibrate(1, [{"question": "limit?", "expected": "12 A"}], strict=True)

    assert result["correct"] == 0  # containment doesn't count under strict


def test_calibrate_fuzzy_tiebreak_passes_near_match(engine, fake_llama):
    engine.ingest_text("facts.txt", DOC)
    fake_llama.scripted = {"temp?": "thermal shutdown at 150 C"}

    result = engine.calibrate(1, [{"question": "temp?", "expected": "thermal shutdown at 150C"}])

    assert result["correct"] == 1  # spacing drift cleared via grounding()'s fuzzy path


def test_calibrate_fuzzy_uses_anchored_window_semantics(engine, fake_llama):
    engine.ingest_text("facts.txt", DOC)
    fake_llama.scripted = {
        "temp?": "Per section 9, the unit enters thermal shutdown at 150 C to protect the cell."
    }

    result = engine.calibrate(1, [{"question": "temp?", "expected": "thermal shutdown at 150C"}])

    # The near-match is embedded in a verbose answer: anchored-window scoring
    # passes it where a whole-string ratio would fail.
    assert result["correct"] == 1


def test_calibrate_unknown_document_raises(engine, fake_llama):
    with pytest.raises(UnknownDocumentError):
        engine.calibrate(999, [{"question": "q", "expected": "e"}])
    assert fake_llama.called("chat") == []  # fails before any generation


def test_calibrate_runs_through_query_path(engine, fake_llama, fake_db):
    engine.ingest_text("facts.txt", DOC)
    fake_llama.scripted = {"q1": "a", "q2": "b"}

    engine.calibrate(1, [
        {"question": "q1", "expected": "a"},
        {"question": "q2", "expected": "b"},
    ])

    assert len(fake_db.queries) == 2  # one logged query per battery item
    assert all(q["success"] is True for q in fake_db.queries)


# --- regression: non-string quote must not crash verify (code-review find) ---

def test_verify_non_string_quote_does_not_crash(engine, fake_llama):
    engine.ingest_text("facts.txt", DOC)
    # A schema slip yields a numeric quote; grounding() would call .strip() on an
    # int and 500. It must be coerced to "" and the endpoint stay well-formed.
    fake_llama.answer_json = '{"verdict":"supported","quote":42,"conditions":7}'

    result = engine.verify_claim("anything")  # must not raise

    assert result["quote"] == ""          # non-string coerced away
    assert result["conditions"] == ""
    assert result["quote_grounded"] is None  # nothing to ground
