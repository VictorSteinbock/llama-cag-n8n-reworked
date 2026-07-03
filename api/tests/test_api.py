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


# --- F1/F3: POST /verify ----------------------------------------------------

def _verdict_json(verdict, quote, conditions=""):
    import json
    return json.dumps(
        {"claim": "c", "verdict": verdict, "quote": quote, "conditions": conditions}
    )


def test_verify_endpoint_happy_path(client, fake_llama):
    client.post("/documents/text", json={"text": "Fredville is the capital of Freedonia."})
    fake_llama.answer_json = _verdict_json("supported", "Fredville is the capital")

    response = client.post("/verify", json={"claim": "Fredville is the capital"})

    assert response.status_code == 200
    body = response.json()
    assert body["verdict"] == "supported"
    assert body["quote_grounded"] is True
    assert body["grounding_method"] == "exact"
    assert body["match_ratio"] == 1.0
    assert body["conditions"] == ""
    assert body["document"]["id"] == 1


def test_verify_unknown_document_is_404(client):
    client.post("/documents/text", json={"text": "something"})
    response = client.post("/verify", json={"claim": "x", "document_id": 42})
    assert response.status_code == 404


def test_verify_no_documents_is_409(client):
    response = client.post("/verify", json={"claim": "anything"})
    assert response.status_code == 409


def test_verify_validation_is_422(client):
    assert client.post("/verify", json={}).status_code == 422
    assert client.post("/verify", json={"claim": ""}).status_code == 422


def test_verify_non_json_is_error_not_500(client, fake_llama):
    client.post("/documents/text", json={"text": "Fredville is the capital."})
    fake_llama.answer_json = "sorry, I can't answer that"

    response = client.post("/verify", json={"claim": "x"})

    assert response.status_code == 200
    assert response.json()["verdict"] == "error"


def test_verify_llama_outage_is_502(client, fake_llama):
    from app.llama import LlamaError

    client.post("/documents/text", json={"text": "Fredville is the capital."})

    def boom(*a, **k):
        raise LlamaError("connection refused")

    fake_llama.chat = boom
    response = client.post("/verify", json={"claim": "x"})
    assert response.status_code == 502


def test_verify_doc_deleted_mid_flight_is_404(client, fake_llama, fake_db):
    client.post("/documents/text", json={"text": "Fredville is the capital."})
    original_chat = fake_llama.chat

    def chat_then_delete(*a, **k):
        result = original_chat(*a, **k)
        fake_db.documents.pop(1, None)  # row vanishes before the grounding re-fetch
        return result

    fake_llama.chat = chat_then_delete
    fake_llama.answer_json = _verdict_json("supported", "Fredville is the capital")

    response = client.post("/verify", json={"claim": "x"})
    assert response.status_code == 404  # the doc-is-None guard, never a 500


# --- F5: GET /stats ---------------------------------------------------------

def test_index_lists_stats_endpoint(client):
    assert any("GET /stats" in e for e in client.get("/").json()["endpoints"])


def test_stats_endpoint_returns_windows_and_savings(client):
    client.post("/documents/text", json={"text": "Fredville is the capital."})
    client.post("/query", json={"question": "q1?"})
    client.post("/query", json={"question": "q2?"})

    body = client.get("/stats").json()

    assert body["windows"]["all"]["queries"] == 2
    assert body["windows"]["all"]["tokens_reused"] == 2 * 480  # fake chat cache_n
    assert body["savings"]["is_estimate"] is True


def test_stats_hides_money_line_when_price_zero(client):
    client.post("/documents/text", json={"text": "Fredville is the capital."})
    client.post("/query", json={"question": "q?"})

    savings = client.get("/stats").json()["savings"]

    assert savings["estimated_usd"] is None
    assert savings["cloud_price_per_1k_input"] == 0.0


def test_stats_shows_savings_when_price_set(fake_llama, fake_db, tmp_path):
    from app.cag import CagEngine
    from app.config import Settings

    settings = Settings(
        cache_dir=tmp_path, llama_ctx_size=1000, answer_reserve_tokens=100,
        db_password="test", cloud_price_per_1k_input=0.003,
    )
    engine = CagEngine(fake_llama, fake_db, settings)
    with TestClient(create_app(engine=engine)) as priced_client:
        priced_client.post("/documents/text", json={"text": "Fredville is the capital."})
        priced_client.post("/query", json={"question": "q?"})
        savings = priced_client.get("/stats").json()["savings"]

    assert savings["estimated_usd"] == round(480 / 1000 * 0.003, 4)


# --- F4: POST /documents/{id}/calibrate ------------------------------------

def test_calibrate_endpoint_happy_path(client, fake_llama):
    client.post("/documents/text", json={"text": "Fredville is the capital."})
    fake_llama.scripted = {"q1": "Fredville", "q2": "wrong"}

    response = client.post("/documents/1/calibrate", json={"qa": [
        {"question": "q1", "expected": "Fredville"},
        {"question": "q2", "expected": "Metropolis"},
    ]})

    assert response.status_code == 200
    body = response.json()
    assert body["n"] == 2
    assert body["correct"] == 1
    assert body["accuracy"] == 0.5
    assert body["document"]["id"] == 1
    assert len(body["misses"]) == 1


def test_calibrate_unknown_document_is_404(client):
    client.post("/documents/text", json={"text": "something"})
    response = client.post(
        "/documents/999/calibrate", json={"qa": [{"question": "q", "expected": "e"}]}
    )
    assert response.status_code == 404


def test_calibrate_empty_battery_is_422(client):
    client.post("/documents/text", json={"text": "something"})
    response = client.post("/documents/1/calibrate", json={"qa": []})
    assert response.status_code == 422


def test_calibrate_over_cap_is_422(fake_llama, fake_db, tmp_path):
    from app.cag import CagEngine
    from app.config import Settings

    settings = Settings(
        cache_dir=tmp_path, llama_ctx_size=1000, answer_reserve_tokens=100,
        db_password="test", calibrate_max_items=2,
    )
    engine = CagEngine(fake_llama, fake_db, settings)
    with TestClient(create_app(engine=engine)) as capped_client:
        capped_client.post("/documents/text", json={"text": "Fredville is the capital."})
        response = capped_client.post("/documents/1/calibrate", json={"qa": [
            {"question": "q1", "expected": "a"},
            {"question": "q2", "expected": "b"},
            {"question": "q3", "expected": "c"},
        ]})

    assert response.status_code == 422
    assert "CALIBRATE_MAX_ITEMS" in response.json()["detail"]


# --- F9: zero-install web UI at /ui ----------------------------------------

def test_webui_served_at_ui(client):
    response = client.get("/ui/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert 'id="view"' in response.text  # the SPA's swap target


def test_webui_index_is_self_contained():
    import pathlib
    import re

    html = (pathlib.Path(__file__).resolve().parents[1] / "app" / "webui" / "index.html").read_text(
        encoding="utf-8"
    )
    # No external resource loads: every src/href must be relative or data:, never
    # a CDN/font/script URL (keeps the page CSP-clean and offline).
    assert not re.search(r'(?:src|href)\s*=\s*["\']https?://', html, re.I)


def test_webui_disabled_returns_404(fake_llama, fake_db, tmp_path):
    from app.cag import CagEngine
    from app.config import Settings

    settings = Settings(
        cache_dir=tmp_path, llama_ctx_size=1000, answer_reserve_tokens=100,
        db_password="test", webui_enabled=False,
    )
    engine = CagEngine(fake_llama, fake_db, settings)
    with TestClient(create_app(engine=engine)) as disabled_client:
        assert disabled_client.get("/ui/").status_code == 404


def test_webui_index_js_parses_if_node_available():
    # Guard against a syntax error shipping in the SPA's inline script (which the
    # served/self-contained tests can't catch — they never execute the JS). Runs
    # `node --check`; skips cleanly where node isn't on PATH (e.g. python-only CI).
    import pathlib
    import re
    import subprocess
    import tempfile

    html = (pathlib.Path(__file__).resolve().parents[1] / "app" / "webui" / "index.html").read_text(
        encoding="utf-8"
    )
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    js = max(scripts, key=len)  # the SPA logic is the largest script block
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(js)
        js_path = f.name
    try:
        proc = subprocess.run(
            ["node", "--check", js_path], capture_output=True, text=True
        )
    except (FileNotFoundError, OSError):
        pytest.skip("node not available")
    finally:
        pathlib.Path(js_path).unlink(missing_ok=True)
    assert proc.returncode == 0, proc.stderr
