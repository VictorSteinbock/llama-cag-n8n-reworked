import pytest

from app.cag import CagEngine, DocumentTooLargeError, NoCachedDocumentError

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


def test_ingest_rejects_documents_larger_than_context(engine, fake_llama, fake_db):
    fake_llama.tokens_per_text = 950  # > 1000 - 100 reserve

    with pytest.raises(DocumentTooLargeError) as exc:
        engine.ingest_text("big.txt", DOC)

    assert exc.value.limit == 900
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
    engine.ingest_text("facts.txt", DOC)
    (tmp_path / "orphan.bin").write_bytes(b"junk")
    fake_db.documents[1]["cache_file"] = "gone.bin"  # db points at a lost file

    report = engine.maintenance()

    assert report["orphan_files_removed"] == ["doc-1.bin", "orphan.bin"]
    assert report["missing_cache_files"] == ["gone.bin"]
    assert not (tmp_path / "orphan.bin").exists()


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
    fake_llama.tokens_per_text = 450  # fits 1000-100 but not 1000/2-100

    with pytest.raises(DocumentTooLargeError) as exc:
        engine.ingest_text("big.txt", DOC)
    assert exc.value.limit == 400
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
