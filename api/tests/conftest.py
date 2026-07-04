"""Test doubles: an in-memory Database and a scripted LlamaClient.

They implement exactly the surface CagEngine uses, so engine and API tests run
with no Postgres and no llama-server.
"""

import datetime as dt
from pathlib import Path

import pytest

from app.cag import CagEngine
from app.config import Settings
from app.llama import LlamaError


class FakeDatabase:
    def __init__(self):
        self.documents: dict[int, dict] = {}
        self.queries: list[dict] = []
        self._next_id = 1
        self.ping_ok = True
        # When True, the next insert_document simulates losing a concurrent
        # insert race: the row that now exists belongs to the "winner", and
        # insert returns None (the real Database swallows UniqueViolation).
        self.conflict_on_insert = False

    # -- surface used by CagEngine ------------------------------------------
    def ping(self):
        if not self.ping_ok:
            raise ConnectionError("db down")
        return True

    def find_by_sha256(self, sha256):
        for doc in self.documents.values():
            if doc["content_sha256"] == sha256:
                return self._public(doc)
        return None

    def insert_document(self, slug, file_name, content, sha256):
        doc = {
            "id": self._next_id,
            "slug": slug,
            "file_name": file_name,
            "content": content,
            "content_sha256": sha256,
            "n_tokens": None,
            "cache_file": None,
            "status": "pending",
            "error": None,
            "created_at": dt.datetime.now(dt.UTC),
            "cached_at": None,
            "last_used_at": None,
            "use_count": 0,
        }
        self.documents[self._next_id] = doc
        self._next_id += 1
        if self.conflict_on_insert:
            self.conflict_on_insert = False
            return None  # the row above plays the concurrent winner's
        return self._public(doc)

    def get_document(self, document_id, *, with_content=False):
        doc = self.documents.get(document_id)
        return self._public(doc, with_content) if doc else None

    def latest_cached(self, *, with_content=False):
        cached = [d for d in self.documents.values() if d["status"] == "cached"]
        if not cached:
            return None
        doc = max(cached, key=lambda d: (d["cached_at"], d["id"]))
        return self._public(doc, with_content)

    def list_documents(self):
        return [self._public(d) for d in self.documents.values()]

    def mark_cached(self, document_id, n_tokens, cache_file):
        # Mirror the real UPDATE ... RETURNING id: a missing row (deleted while
        # the caller warmed/recomputed) updates nothing and returns False.
        doc = self.documents.get(document_id)
        if doc is None:
            return False
        doc.update(
            status="cached", n_tokens=n_tokens, cache_file=cache_file,
            cached_at=dt.datetime.now(dt.UTC), error=None,
        )
        return True

    def mark_failed(self, document_id, error):
        # Mirror the real UPDATE ... WHERE id = %s: a missing row is a no-op, not
        # a KeyError (a delete can race an ingest failure).
        doc = self.documents.get(document_id)
        if doc is not None:
            doc.update(status="failed", error=error)

    def touch_used(self, document_id):
        # Mirror the real UPDATE ... WHERE id = %s: a missing row (e.g. deleted
        # by a concurrent request mid-query) updates nothing and does not raise.
        doc = self.documents.get(document_id)
        if doc is None:
            return
        doc["use_count"] += 1
        doc["last_used_at"] = dt.datetime.now(dt.UTC)

    def delete_document(self, document_id):
        doc = self.documents.pop(document_id, None)
        if doc is None:
            return None
        return {"id": doc["id"], "cache_file": doc["cache_file"]}

    def all_cache_files(self):
        return {d["cache_file"] for d in self.documents.values() if d["cache_file"]}

    def log_query(self, **kwargs):
        self.queries.append(kwargs)

    def stats(self):
        return {
            "documents": len(self.documents),
            "cached_documents": sum(
                1 for d in self.documents.values() if d["status"] == "cached"
            ),
            "queries_24h": len(self.queries),
            "avg_duration_ms_24h": 0,
        }

    def usage_stats(self):
        # The fake logs no timestamp, so it collapses all three windows to the
        # same totals over self.queries (window differentiation is a live-DB
        # concern, out of scope for the fake). Same key shape as the real method.
        reused = sum((q.get("n_cached_tokens") or 0) for q in self.queries)
        evaluated = sum((q.get("n_eval_tokens") or 0) for q in self.queries)
        failed = sum(1 for q in self.queries if not q.get("success"))
        n = len(self.queries)
        denom = reused + evaluated
        window = {
            "queries": n,
            "failed": failed,
            "tokens_reused": reused,
            "tokens_evaluated": evaluated,
            "avg_eval_tokens": round(evaluated / n, 4) if n else 0.0,
            "reuse_ratio": round(reused / denom, 4) if denom else 0.0,
            "p50_duration_ms": 0,
            "p95_duration_ms": 0,
        }
        return {"24h": dict(window), "7d": dict(window), "all": dict(window)}

    @staticmethod
    def _public(doc, with_content=False):
        skip = () if with_content else ("content",)
        return {k: v for k, v in doc.items() if k not in skip}


class FakeLlama:
    """Scripted llama-server. slot_save/restore actually touch files so the
    engine's on-disk existence checks are exercised for real."""

    def __init__(self, cache_dir: Path, tokens_per_text: int = 50):
        self.cache_dir = cache_dir
        self.tokens_per_text = tokens_per_text
        self.calls: list[tuple] = []
        self.fail_restore = False
        self.healthy = True
        self.answer = "the answer"
        # When set, chat() returns this instead of `answer` — used by /verify
        # tests to feed a JSON verdict string. Default None keeps every existing
        # test (which asserts answer == "the answer") unchanged.
        self.answer_json: str | None = None
        # Per-question scripted answers keyed on the last user turn — used by
        # calibration tests. Empty dict falls back to answer_json-else-answer.
        self.scripted: dict[str, str] = {}
        self.model_path = "/models/fake-model.gguf"

    def health(self):
        if not self.healthy:
            raise LlamaError("llama-server unreachable")
        return {"status": "ok"}

    def props(self):
        if not self.healthy:
            raise LlamaError("llama-server unreachable")
        self.calls.append(("props",))
        return {"model_path": self.model_path, "total_slots": 1}

    def count_tokens(self, text):
        self.calls.append(("count_tokens", len(text)))
        return self.tokens_per_text

    def chat(self, messages, *, max_tokens, temperature, slot_id=0, warm=False, json_schema=None):
        self.calls.append(("chat", messages[0]["content"][:60], warm, slot_id))
        self.last_messages = messages
        self.last_json_schema = json_schema
        # Canonical composed body: scripted (per last user turn) wins, else
        # answer_json if set, else answer.
        fallback = self.answer_json if self.answer_json is not None else self.answer
        content = self.scripted.get(messages[-1]["content"], fallback)
        return {
            "content": content,
            "timings": {"prompt_n": 12, "cache_n": 480, "predicted_n": 20},
            "usage": {"prompt_tokens": 492},
        }

    def slot_save(self, filename, slot_id=0):
        self.calls.append(("slot_save", filename, slot_id))
        (self.cache_dir / filename).write_bytes(b"\x00kv")
        return {"n_saved": self.tokens_per_text, "filename": filename}

    def slot_restore(self, filename, slot_id=0):
        self.calls.append(("slot_restore", filename, slot_id))
        if self.fail_restore:
            raise LlamaError("restore failed")
        # Mirror the real server: restoring a file that doesn't exist under
        # --slot-save-path is a 400, never a silent success.
        if not (self.cache_dir / filename).exists():
            raise LlamaError(f"slot restore: file not found: {filename}")
        return {"n_restored": self.tokens_per_text, "filename": filename}

    def slot_erase(self, slot_id=0):
        self.calls.append(("slot_erase", slot_id))
        return {"n_erased": 0}

    def called(self, name):
        return [c for c in self.calls if c[0] == name]


@pytest.fixture
def settings(tmp_path):
    return Settings(
        cache_dir=tmp_path,
        llama_ctx_size=1000,
        answer_reserve_tokens=100,
        # The query budget counts the ACTUAL answer allowance (max_tokens),
        # so the test default must match the shrunken reserve — the production
        # defaults keep the same 1024/1024 pairing.
        default_max_answer_tokens=100,
        db_password="test",
    )


@pytest.fixture
def fake_db():
    return FakeDatabase()


@pytest.fixture
def fake_llama(tmp_path):
    return FakeLlama(cache_dir=tmp_path)


@pytest.fixture
def engine(fake_llama, fake_db, settings):
    return CagEngine(fake_llama, fake_db, settings)
