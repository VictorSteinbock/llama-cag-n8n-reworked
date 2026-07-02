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
        doc = self.documents[document_id]
        doc.update(
            status="cached", n_tokens=n_tokens, cache_file=cache_file,
            cached_at=dt.datetime.now(dt.UTC), error=None,
        )

    def mark_failed(self, document_id, error):
        self.documents[document_id].update(status="failed", error=error)

    def touch_used(self, document_id):
        doc = self.documents[document_id]
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

    def health(self):
        if not self.healthy:
            raise LlamaError("llama-server unreachable")
        return {"status": "ok"}

    def count_tokens(self, text):
        self.calls.append(("count_tokens", len(text)))
        return self.tokens_per_text

    def chat(self, messages, *, max_tokens, temperature, slot_id=0, warm=False):
        self.calls.append(("chat", messages[0]["content"][:60], warm, slot_id))
        self.last_messages = messages
        return {
            "content": self.answer,
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
