"""API contract tests: real FastAPI app + real CagEngine over the fakes."""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client(engine):
    with TestClient(create_app(engine=engine)) as test_client:
        yield test_client


def test_index_lists_endpoints(client):
    body = client.get("/").json()
    assert body["service"] == "cag-api"
    assert any("/query" in e for e in body["endpoints"])


def test_ingest_text_then_query_roundtrip(client):
    created = client.post(
        "/documents/text", json={"text": "Fredville is the capital.", "file_name": "facts.txt"}
    )
    assert created.status_code == 201
    doc = created.json()
    assert doc["status"] == "cached"
    assert "content" not in doc  # never echoed back

    answer = client.post("/query", json={"question": "What is the capital?"})
    assert answer.status_code == 200
    body = answer.json()
    assert body["answer"] == "the answer"
    assert body["document"]["id"] == doc["id"]


def test_ingest_multipart_upload(client):
    response = client.post(
        "/documents", files={"file": ("notes.md", b"# Facts\nsome text", "text/markdown")}
    )
    assert response.status_code == 201
    assert response.json()["file_name"] == "notes.md"

    listed = client.get("/documents").json()["documents"]
    assert [d["file_name"] for d in listed] == ["notes.md"]


def test_unsupported_upload_is_415(client):
    response = client.post("/documents", files={"file": ("evil.exe", b"MZ\x00\x01")})
    assert response.status_code == 415


@pytest.fixture
def tiny_upload_client(fake_llama, fake_db, tmp_path):
    # Same wiring as the main client, but with a 1 MB upload cap so the
    # oversize path is testable without a 50 MB body.
    from app.cag import CagEngine
    from app.config import Settings

    settings = Settings(
        cache_dir=tmp_path, llama_ctx_size=1000, answer_reserve_tokens=100,
        db_password="test", max_upload_mb=1,
    )
    engine = CagEngine(fake_llama, fake_db, settings)
    with TestClient(create_app(engine=engine)) as test_client:
        yield test_client


def test_oversized_upload_is_413_naming_the_knob(tiny_upload_client):
    big = b"x" * (1024 * 1024 + 1)  # one byte over the 1 MB cap
    response = tiny_upload_client.post(
        "/documents", files={"file": ("big.txt", big, "text/plain")}
    )
    assert response.status_code == 413
    assert "MAX_UPLOAD_MB" in response.json()["detail"]


def test_upload_under_cap_is_unaffected(tiny_upload_client):
    response = tiny_upload_client.post(
        "/documents", files={"file": ("ok.txt", b"small file body", "text/plain")}
    )
    assert response.status_code == 201


def test_too_large_document_is_413(client, fake_llama):
    fake_llama.tokens_per_text = 5000
    response = client.post("/documents/text", json={"text": "x" * 100})
    assert response.status_code == 413
    # 1000 ctx − 100 answer reserve − 96 prompt overhead
    assert response.json()["limit"] == 804


def test_query_with_no_documents_is_409(client):
    response = client.post("/query", json={"question": "hello?"})
    assert response.status_code == 409


def test_query_unknown_document_is_404(client):
    client.post("/documents/text", json={"text": "something"})
    response = client.post("/query", json={"question": "q", "document_id": 42})
    assert response.status_code == 404


def test_query_validation_is_422(client):
    assert client.post("/query", json={}).status_code == 422
    assert client.post("/query", json={"question": ""}).status_code == 422
    assert (
        client.post("/query", json={"question": "q", "temperature": 9}).status_code == 422
    )
    bad_role = [{"role": "system", "content": "sneaky"}]
    assert (
        client.post("/query", json={"question": "q", "history": bad_role}).status_code == 422
    )


def test_query_accepts_json_schema(client, fake_llama):
    client.post("/documents/text", json={"text": "Fredville is the capital."})
    schema = {
        "type": "object",
        "properties": {"verdict": {"type": "string"}},
        "required": ["verdict"],
    }
    response = client.post(
        "/query", json={"question": "What is the capital?", "json_schema": schema}
    )
    assert response.status_code == 200
    # The schema reached the llama layer unchanged (sampling-only passthrough).
    assert fake_llama.last_json_schema == schema


def test_query_rejects_non_object_json_schema_with_422(client):
    client.post("/documents/text", json={"text": "something"})
    response = client.post(
        "/query", json={"question": "q", "json_schema": "not-an-object"}
    )
    assert response.status_code == 422


def test_query_accepts_conversation_history(client):
    client.post("/documents/text", json={"text": "Fredville facts."})
    response = client.post(
        "/query",
        json={
            "question": "And?",
            "history": [
                {"role": "user", "content": "Hi"},
                {"role": "assistant", "content": "Hello"},
            ],
        },
    )
    assert response.status_code == 200
    assert response.json()["answer"] == "the answer"


def test_delete_document(client):
    doc_id = client.post("/documents/text", json={"text": "bye"}).json()["id"]
    assert client.delete(f"/documents/{doc_id}").status_code == 200
    assert client.delete(f"/documents/{doc_id}").status_code == 404


def test_llama_outage_is_502(client, fake_llama):
    from app.llama import LlamaError

    def boom(*a, **k):
        raise LlamaError("connection refused")

    fake_llama.chat = boom
    response = client.post("/documents/text", json={"text": "doomed"})
    assert response.status_code == 502


def test_health_degraded_is_503(client, fake_llama):
    assert client.get("/health").status_code == 200
    fake_llama.healthy = False
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["status"] == "degraded"


def test_maintenance_endpoint(client):
    client.post("/documents/text", json={"text": "keep me"})
    report = client.post("/maintenance")
    assert report.status_code == 200
    assert report.json()["cached_documents"] == 1
