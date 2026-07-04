"""Tool-level tests: call the FastMCP tool functions directly (not over stdio).

Each tool's happy path plus the error messages the agent will actually read:
409 (nothing cached), 413 (too large), 415 (unsupported), and an unreachable
stack. The fake cag-api lives in conftest.py.
"""

from __future__ import annotations

import httpx

from cag_mcp.server import ask_document, ingest_file, ingest_text, list_documents, verify

# --- list_documents --------------------------------------------------------


def test_list_documents_happy_path(fake_api):
    fake_api.documents = [
        {"id": 1, "file_name": "manual.pdf", "status": "cached",
         "n_tokens": 28400, "last_used_at": "2026-07-02T10:00:00Z"},
        {"id": 2, "file_name": "draft.txt", "status": "pending",
         "n_tokens": None, "last_used_at": None},
    ]
    out = list_documents()

    assert "manual.pdf" in out
    assert "cached" in out
    assert "28,400" in out  # thousands-formatted token count
    assert "draft.txt" in out
    assert "pending" in out


def test_list_documents_empty(fake_api):
    out = list_documents()
    assert "No documents" in out
    assert "ingest" in out.lower()


def test_list_documents_unreachable(fake_api):
    fake_api.fail_connection("GET", "/documents")
    out = list_documents()
    assert "llamacag.py start" in out
    assert "does not appear to be running" in out


# --- ask_document ----------------------------------------------------------


def test_ask_document_happy_path_with_provenance(fake_api):
    out = ask_document("What are the safety limits in section 4?")

    # The answer comes first.
    assert out.startswith("The safety limit is 8 A continuous.")
    # Then a single compact provenance line with all the pieces.
    line = out.strip().splitlines()[-1]
    assert line.startswith("[") and line.endswith("]")
    assert "doc 7 manual.pdf" in line
    assert "cache: memory" in line
    # evaluated (43) of total prompt tokens (43 + 28400 = 28,443), comma-grouped
    assert "evaluated 43 of 28,443 prompt tokens" in line
    assert "640 ms" in line


def test_ask_document_passes_document_id_and_max_tokens(fake_api):
    ask_document("q", document_id=5, max_tokens=256)
    body = fake_api.requests[-1].content
    import json

    payload = json.loads(body)
    assert payload["document_id"] == 5
    assert payload["max_tokens"] == 256


def test_ask_document_forwards_json_schema(fake_api):
    schema = {
        "type": "object",
        "properties": {
            "claim": {"type": "string"},
            "verdict": {"enum": ["supported", "absent", "contradicted"]},
            "quote": {"type": "string"},
        },
        "required": ["claim", "verdict", "quote"],
    }
    ask_document("verify this", json_schema=schema)
    import json

    payload = json.loads(fake_api.requests[-1].content)
    assert payload["json_schema"] == schema


def test_ask_document_409_tells_caller_to_ingest(fake_api):
    fake_api.set_response(
        "POST", "/query",
        httpx.Response(409, json={"detail": "No cached documents yet — ingest one first."}),
    )
    out = ask_document("anyone home?")
    assert "Ingest a document first" in out
    assert "ingest_file or ingest_text" in out


def test_ask_document_404_points_at_list(fake_api):
    fake_api.set_response(
        "POST", "/query",
        httpx.Response(404, json={"detail": "No document with id 42"}),
    )
    out = ask_document("q", document_id=42)
    assert "No document with id 42" in out
    assert "list_documents" in out


def test_ask_document_unreachable(fake_api):
    fake_api.fail_connection("POST", "/query")
    out = ask_document("q")
    assert "llamacag.py start" in out


# --- ingest_text -----------------------------------------------------------


def test_ingest_text_happy_path(fake_api):
    out = ingest_text("spec.md", "some specification text")
    assert "document 1" in out
    assert "cached" in out
    assert "1,234 tokens" in out
    # And it actually sent file_name + text.
    import json

    payload = json.loads(fake_api.requests[-1].content)
    assert payload["file_name"] == "spec.md"
    assert payload["text"] == "some specification text"


def test_ingest_text_413_surfaced_verbatim(fake_api):
    detail = ("Document is 50000 tokens but the per-slot limit is 64512 "
              "(LLAMA_CTX_SIZE=65536). Raise LLAMA_CTX_SIZE in .env and restart.")
    fake_api.set_response("POST", "/documents/text", httpx.Response(413, json={"detail": detail}))
    out = ingest_text("big.md", "x" * 100)
    assert "413" in out
    assert detail in out  # verbatim


def test_ingest_text_unreachable(fake_api):
    fake_api.fail_connection("POST", "/documents/text")
    out = ingest_text("spec.md", "text")
    assert "llamacag.py start" in out


# --- ingest_file -----------------------------------------------------------


def test_ingest_file_happy_path(fake_api, tmp_path):
    f = tmp_path / "manual.txt"
    f.write_text("the manual body", encoding="utf-8")
    out = ingest_file(str(f))
    assert "document 2" in out
    assert "28,400 tokens" in out


def test_ingest_file_missing_path(fake_api, tmp_path):
    out = ingest_file(str(tmp_path / "nope.txt"))
    assert "No file exists" in out
    # Never even called the API.
    assert fake_api.requests == []


def test_ingest_file_directory_rejected(fake_api, tmp_path):
    out = ingest_file(str(tmp_path))
    assert "not a file" in out
    assert fake_api.requests == []


def test_ingest_file_empty_rejected(fake_api, tmp_path):
    f = tmp_path / "empty.txt"
    f.write_bytes(b"")
    out = ingest_file(str(f))
    assert "empty" in out
    assert fake_api.requests == []


def test_ingest_file_oversize_rejected(fake_api, tmp_path, monkeypatch):
    from cag_mcp import server

    monkeypatch.setattr(server, "MAX_FILE_BYTES", 10)
    f = tmp_path / "big.txt"
    f.write_bytes(b"x" * 50)
    out = ingest_file(str(f))
    assert "larger than" in out
    assert fake_api.requests == []


def test_ingest_file_415_surfaced_verbatim(fake_api, tmp_path):
    detail = "Unsupported file type '.zip'. Supported: .htm, .html, .md, .pdf, .txt"
    fake_api.set_response("POST", "/documents", httpx.Response(415, json={"detail": detail}))
    f = tmp_path / "archive.zip"
    f.write_bytes(b"PK\x03\x04payload")
    out = ingest_file(str(f))
    assert "415" in out
    assert detail in out  # verbatim


def test_ingest_file_413_surfaced_verbatim(fake_api, tmp_path):
    detail = "Document is 90000 tokens but the per-slot limit is 64512 (LLAMA_CTX_SIZE=65536)."
    fake_api.set_response("POST", "/documents", httpx.Response(413, json={"detail": detail}))
    f = tmp_path / "huge.pdf"
    f.write_bytes(b"%PDF-1.4 lots of text")
    out = ingest_file(str(f))
    assert "413" in out
    assert detail in out  # verbatim


def test_ingest_file_unreachable(fake_api, tmp_path):
    fake_api.fail_connection("POST", "/documents")
    f = tmp_path / "manual.txt"
    f.write_text("body", encoding="utf-8")
    out = ingest_file(str(f))
    assert "llamacag.py start" in out


# --- verify (F1b) ----------------------------------------------------------


def test_verify_happy_path_grounded(fake_api):
    out = verify("The peak current limit is 12 A")

    assert "verdict: supported" in out
    assert "peak at 12 A for 10 s" in out
    assert "quote_grounded: yes" in out
    # provenance line still present
    line = out.strip().splitlines()[-1]
    assert line.startswith("[") and line.endswith("]")
    assert "cache: memory" in line


def test_verify_catches_fabricated_quote(fake_api):
    fake_api.verdict = "supported"
    fake_api.quote = "the warranty covers water damage forever"
    fake_api.quote_grounded = False
    fake_api.grounding_method = "fuzzy"
    fake_api.match_ratio = 0.31

    out = verify("The warranty covers water damage")

    # The verdict claims support, but grounding says the quote is fabricated —
    # the load-bearing signal must be loud.
    assert "verdict: supported" in out
    assert "quote_grounded: NO" in out
    assert "fabricated" in out


def test_verify_surfaces_conditions(fake_api):
    fake_api.verdict = "contradicted"
    fake_api.conditions = "only if the item is defective"

    out = verify("Widgets are refundable within 30 days")

    assert "conditions: only if the item is defective" in out


def test_verify_absent_is_not_grounded(fake_api):
    fake_api.verdict = "absent"
    fake_api.quote = ""
    fake_api.quote_grounded = None
    fake_api.grounding_method = "absent"
    fake_api.match_ratio = 0.0

    out = verify("The document mentions dragons")

    assert "verdict: absent" in out
    assert "quote_grounded: n/a" in out


def test_verify_no_documents_is_guided(fake_api):
    fake_api.set_response("POST", "/verify", httpx.Response(409, json={"detail": "nothing cached"}))
    out = verify("anything")
    assert "Ingest a document first" in out


def test_verify_unknown_document_is_guided(fake_api):
    fake_api.set_response(
        "POST", "/verify", httpx.Response(404, json={"detail": "No document with id 99"})
    )
    out = verify("anything", document_id=99)
    assert "list_documents" in out


def test_verify_unreachable_stack(fake_api):
    fake_api.fail_connection("POST", "/verify")
    out = verify("anything")
    assert "llamacag.py start" in out
    assert "does not appear to be running" in out
