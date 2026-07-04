"""Unit tests for db.py logic that does not need a live Postgres.

Database() opens no connection at construction (open=False), so we can drive
its methods with a stubbed _one and assert the parameterized-SQL behaviour that
matters — here, that log_query survives a document being deleted mid-query.
"""

from psycopg.errors import ForeignKeyViolation, UniqueViolation

from app.db import Database


def _db() -> Database:
    return Database("host=nowhere port=5432 dbname=x user=y password=z")


def test_log_query_happy_path_passes_document_id():
    db = _db()
    seen: list[tuple] = []

    def one(sql, params=()):
        seen.append(params)
        return {"id": 1}

    db._one = one
    db.log_query(document_id=7, question="q", answer="a", success=True, duration_ms=5)

    assert len(seen) == 1
    assert seen[0][0] == 7  # document_id is the first bound parameter


def test_log_query_retries_with_null_when_document_deleted_mid_query():
    # A delete racing a concurrent query removes the document row before the
    # query logs its result; the FK insert fails. log_query must retry with a
    # NULL document_id (matching ON DELETE SET NULL) rather than propagate.
    db = _db()
    seen: list[tuple] = []

    def one(sql, params=()):
        seen.append(params)
        if len(seen) == 1:
            raise ForeignKeyViolation("query_log_document_id_fkey")
        return {"id": 1}

    db._one = one
    db.log_query(
        document_id=7, question="q", answer="a", success=True,
        n_prompt_tokens=10, duration_ms=5,
    )

    assert len(seen) == 2  # first attempt failed, retried once
    assert seen[0][0] == 7  # first attempt used the (now gone) document id
    assert seen[1][0] is None  # retry used NULL
    # The rest of the payload is preserved on the retry.
    assert seen[0][1:] == seen[1][1:]


def test_insert_document_returns_none_on_unique_violation():
    # Two concurrent ingests of identical content: the loser's INSERT trips the
    # content_sha256 UNIQUE constraint. That is a dedupe signal, not an error —
    # insert_document reports it as None so the engine can re-fetch the winner.
    db = _db()

    def one(sql, params=()):
        raise UniqueViolation("documents_content_sha256_key")

    db._one = one
    assert db.insert_document("slug", "f.txt", "content", "sha") is None


def test_mark_cached_reports_whether_a_row_was_updated():
    db = _db()

    db._one = lambda sql, params=(): {"id": 7}
    assert db.mark_cached(7, 100, "doc-7.bin") is True

    db._one = lambda sql, params=(): None  # row deleted mid-warm/heal
    assert db.mark_cached(7, 100, "doc-7.bin") is False


def test_log_query_null_document_fk_violation_is_not_swallowed():
    # If a NULL-document insert somehow violates the FK, that is a real error,
    # not the delete-race case, and must propagate.
    db = _db()

    def one(sql, params=()):
        raise ForeignKeyViolation("unexpected")

    db._one = one
    try:
        db.log_query(document_id=None, question="q", answer=None, success=False)
    except ForeignKeyViolation:
        pass
    else:  # pragma: no cover - guard
        raise AssertionError("expected ForeignKeyViolation to propagate")


# --- F5: usage_stats -------------------------------------------------------

def test_usage_stats_shapes_three_windows_and_reuse_ratio():
    db = _db()

    def one(sql, params=()):
        return {"queries": 5, "failed": 0, "tokens_reused": 900,
                "tokens_evaluated": 100, "avg_eval_tokens": 20.0,
                "p50_duration_ms": 10, "p95_duration_ms": 30}

    db._one = one
    result = db.usage_stats()

    assert set(result) == {"24h", "7d", "all"}
    for window in result.values():
        assert window["reuse_ratio"] == 0.9  # 900 / (900 + 100)


def test_usage_stats_reuse_ratio_zero_when_no_tokens():
    db = _db()
    db._one = lambda sql, params=(): {"tokens_reused": 0, "tokens_evaluated": 0}

    result = db.usage_stats()

    assert result["all"]["reuse_ratio"] == 0.0  # no ZeroDivisionError


def test_usage_stats_binds_interval_twice_and_never_concatenates():
    db = _db()
    seen: list[tuple] = []

    def one(sql, params=()):
        seen.append(params)
        return {"tokens_reused": 0, "tokens_evaluated": 0}

    db._one = one
    db.usage_stats()

    # Interval bound (not concatenated), passed twice; None for the all-time window.
    assert seen == [("24 hours", "24 hours"), ("7 days", "7 days"), (None, None)]
