-- llama-cag-n8n v2 schema (llamacag database).
-- Two tables. Document state transitions are owned by cag-api, not triggers.

CREATE TABLE IF NOT EXISTS documents (
    id             BIGSERIAL PRIMARY KEY,
    slug           TEXT        NOT NULL,
    file_name      TEXT        NOT NULL,
    -- Extracted text. Re-sent as the (cached) prompt prefix on every query,
    -- and the source of truth for self-healing a lost KV cache file.
    content        TEXT        NOT NULL,
    content_sha256 CHAR(64)    NOT NULL UNIQUE,
    n_tokens       INTEGER,
    -- Filename inside llama-server's --slot-save-path volume (doc-<id>.bin).
    cache_file     TEXT,
    status         TEXT        NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'cached', 'failed')),
    error          TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    cached_at      TIMESTAMPTZ,
    last_used_at   TIMESTAMPTZ,
    use_count      INTEGER     NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_documents_status ON documents (status);
CREATE INDEX IF NOT EXISTS idx_documents_cached_at ON documents (cached_at DESC);

CREATE TABLE IF NOT EXISTS query_log (
    id               BIGSERIAL PRIMARY KEY,
    document_id      BIGINT REFERENCES documents (id) ON DELETE SET NULL,
    question         TEXT        NOT NULL,
    answer           TEXT,
    success          BOOLEAN     NOT NULL DEFAULT TRUE,
    error            TEXT,
    -- From llama-server timings: how many prompt tokens were actually
    -- evaluated vs. served from the KV cache. The whole point of CAG is
    -- n_eval_tokens staying small after the first query.
    n_prompt_tokens  INTEGER,
    n_cached_tokens  INTEGER,
    n_eval_tokens    INTEGER,
    duration_ms      INTEGER,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_query_log_created_at ON query_log (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_query_log_document_id ON query_log (document_id);
