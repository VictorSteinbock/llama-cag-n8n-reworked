"""Postgres persistence. Parameterized SQL only — no string-built queries."""

from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

DOCUMENT_COLUMNS = (
    "id, slug, file_name, n_tokens, cache_file, status, error, "
    "created_at, cached_at, last_used_at, use_count"
)


class Database:
    def __init__(self, conninfo: str) -> None:
        self._pool = ConnectionPool(
            conninfo, min_size=1, max_size=4, open=False, kwargs={"row_factory": dict_row}
        )

    def open(self, wait_s: float = 60.0) -> None:
        self._pool.open(wait=True, timeout=wait_s)

    def close(self) -> None:
        self._pool.close()

    def _one(self, sql: str, params: tuple = ()) -> dict | None:
        with self._pool.connection() as conn:
            return conn.execute(sql, params).fetchone()

    def _all(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._pool.connection() as conn:
            return conn.execute(sql, params).fetchall()

    def ping(self) -> bool:
        self._one("SELECT 1 AS ok")
        return True

    # --- documents ----------------------------------------------------------

    def insert_document(self, slug: str, file_name: str, content: str, sha256: str) -> dict:
        return self._one(
            f"""
            INSERT INTO documents (slug, file_name, content, content_sha256)
            VALUES (%s, %s, %s, %s)
            RETURNING {DOCUMENT_COLUMNS}
            """,
            (slug, file_name, content, sha256),
        )

    def find_by_sha256(self, sha256: str) -> dict | None:
        return self._one(
            f"SELECT {DOCUMENT_COLUMNS} FROM documents WHERE content_sha256 = %s", (sha256,)
        )

    def get_document(self, document_id: int, *, with_content: bool = False) -> dict | None:
        cols = DOCUMENT_COLUMNS + (", content" if with_content else "")
        return self._one(f"SELECT {cols} FROM documents WHERE id = %s", (document_id,))

    def latest_cached(self, *, with_content: bool = False) -> dict | None:
        cols = DOCUMENT_COLUMNS + (", content" if with_content else "")
        return self._one(
            f"""
            SELECT {cols} FROM documents
            WHERE status = 'cached'
            ORDER BY cached_at DESC NULLS LAST, id DESC
            LIMIT 1
            """
        )

    def list_documents(self) -> list[dict]:
        return self._all(f"SELECT {DOCUMENT_COLUMNS} FROM documents ORDER BY id")

    def mark_cached(self, document_id: int, n_tokens: int, cache_file: str) -> None:
        self._one(
            """
            UPDATE documents
            SET status = 'cached', n_tokens = %s, cache_file = %s,
                cached_at = now(), error = NULL
            WHERE id = %s
            RETURNING id
            """,
            (n_tokens, cache_file, document_id),
        )

    def mark_failed(self, document_id: int, error: str) -> None:
        self._one(
            "UPDATE documents SET status = 'failed', error = %s WHERE id = %s RETURNING id",
            (error[:2000], document_id),
        )

    def touch_used(self, document_id: int) -> None:
        self._one(
            """
            UPDATE documents
            SET last_used_at = now(), use_count = use_count + 1
            WHERE id = %s
            RETURNING id
            """,
            (document_id,),
        )

    def delete_document(self, document_id: int) -> dict | None:
        return self._one(
            "DELETE FROM documents WHERE id = %s RETURNING id, cache_file", (document_id,)
        )

    def all_cache_files(self) -> set[str]:
        rows = self._all("SELECT cache_file FROM documents WHERE cache_file IS NOT NULL")
        return {row["cache_file"] for row in rows}

    # --- query log ----------------------------------------------------------

    def log_query(
        self,
        *,
        document_id: int | None,
        question: str,
        answer: str | None,
        success: bool,
        error: str | None = None,
        n_prompt_tokens: int | None = None,
        n_cached_tokens: int | None = None,
        n_eval_tokens: int | None = None,
        duration_ms: int | None = None,
    ) -> None:
        self._one(
            """
            INSERT INTO query_log (document_id, question, answer, success, error,
                                   n_prompt_tokens, n_cached_tokens, n_eval_tokens, duration_ms)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                document_id,
                question,
                answer,
                success,
                error,
                n_prompt_tokens,
                n_cached_tokens,
                n_eval_tokens,
                duration_ms,
            ),
        )

    def stats(self) -> dict[str, Any]:
        docs = self._one(
            """
            SELECT count(*) AS documents,
                   count(*) FILTER (WHERE status = 'cached') AS cached_documents
            FROM documents
            """
        )
        queries = self._one(
            """
            SELECT count(*) AS queries_24h,
                   coalesce(avg(duration_ms), 0)::int AS avg_duration_ms_24h
            FROM query_log
            WHERE created_at > now() - interval '24 hours'
            """
        )
        return {**docs, **queries}
