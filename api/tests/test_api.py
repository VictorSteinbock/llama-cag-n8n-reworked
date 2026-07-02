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


def test_too_large_document_is_413(client, fake_llama):
    fake_llama.tokens_per_text = 5000
    response = client.post("/documents/text", json={"text": "x" * 100})
    assert response.status_code == 413
    assert response.json()["limit"] == 900


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
