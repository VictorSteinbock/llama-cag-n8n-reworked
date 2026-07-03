"""FastAPI wiring. All logic lives in CagEngine; routes translate HTTP <-> engine."""

import logging
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import __version__
from .cag import (
    CagEngine,
    DocumentTooLargeError,
    NoCachedDocumentError,
    UnknownDocumentError,
)
from .config import Settings
from .db import Database
from .extract import UnsupportedDocumentError
from .llama import LlamaClient, LlamaError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


class TextIngestRequest(BaseModel):
    text: str = Field(min_length=1)
    file_name: str = Field(default="inline.txt", max_length=255)


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1)


class QueryRequest(BaseModel):
    question: str = Field(min_length=1)
    document_id: int | None = None
    max_tokens: int | None = Field(default=None, ge=1, le=8192)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    # Prior conversation turns (oldest first). The document prefix plus the
    # growing history is reused from the KV cache incrementally, so multi-turn
    # chat stays cheap: only the newest tokens are ever evaluated.
    history: list[ChatTurn] | None = Field(default=None, max_length=50)
    # Optional JSON Schema to constrain the answer: llama-server grammar-samples
    # the completion so the reply is valid JSON per this schema. It affects
    # sampling only — the cached document prefix is untouched. A non-object
    # (e.g. a string) is rejected with 422 by the dict typing.
    json_schema: dict | None = None


class VerifyRequest(BaseModel):
    claim: str = Field(min_length=1)
    document_id: int | None = None
    max_tokens: int | None = Field(default=None, ge=1, le=8192)


class CalibrateItem(BaseModel):
    question: str = Field(min_length=1)
    expected: str = Field(min_length=1)


class CalibrateRequest(BaseModel):
    qa: list[CalibrateItem] = Field(min_length=1)  # cap enforced in the route
    strict: bool = False
    max_tokens: int | None = Field(default=None, ge=1, le=8192)


def create_app(engine: CagEngine | None = None) -> FastAPI:
    """App factory. Tests pass a pre-built engine; production builds one from env."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if engine is not None:
            app.state.engine = engine
            yield
            return
        settings = Settings()
        db = Database(settings.db_conninfo)
        db.open(wait_s=60.0)
        llama = LlamaClient(
            settings.llama_server_url,
            query_timeout_s=settings.query_timeout_s,
            warm_timeout_s=settings.warm_timeout_s,
            health_timeout_s=settings.health_timeout_s,
        )
        app.state.engine = CagEngine(llama, db, settings)
        yield
        llama.close()
        db.close()

    app = FastAPI(title="cag-api", version=__version__, lifespan=lifespan)

    def _engine(request: Request) -> CagEngine:
        return request.app.state.engine

    # --- error translation -------------------------------------------------

    @app.exception_handler(UnsupportedDocumentError)
    async def unsupported(_, exc: UnsupportedDocumentError):
        return JSONResponse(status_code=415, content={"detail": str(exc)})

    @app.exception_handler(DocumentTooLargeError)
    async def too_large(_, exc: DocumentTooLargeError):
        return JSONResponse(
            status_code=413,
            content={
                "detail": str(exc),
                "n_tokens": exc.n_tokens,
                "limit": exc.limit,
                "ctx_size": exc.ctx_size,
            },
        )

    @app.exception_handler(NoCachedDocumentError)
    async def no_docs(_, exc: NoCachedDocumentError):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(UnknownDocumentError)
    async def unknown_doc(_, exc: UnknownDocumentError):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(LlamaError)
    async def llama_down(_, exc: LlamaError):
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    # --- routes -------------------------------------------------------------

    @app.get("/")
    def index():
        return {
            "service": "cag-api",
            "version": __version__,
            "endpoints": [
                "GET /health",
                "POST /documents (multipart file)",
                "POST /documents/text {text, file_name?}",
                "GET /documents",
                "DELETE /documents/{id}",
                "POST /query {question, document_id?, max_tokens?, temperature?, history?, "
                "json_schema?}",
                "POST /verify {claim, document_id?, max_tokens?}",
                "POST /documents/{id}/calibrate {qa:[{question, expected}], strict?, max_tokens?}",
                "GET /stats",
                "POST /maintenance",
            ],
        }

    @app.get("/health")
    def health(request: Request):
        report = _engine(request).health()
        status = 200 if report["status"] == "ok" else 503
        return JSONResponse(status_code=status, content=report)

    @app.post("/documents", status_code=201)
    def ingest_file(request: Request, file: UploadFile):
        engine = _engine(request)
        limit_mb = engine.settings.max_upload_mb
        data = _read_limited(file.file, limit_mb * 1024 * 1024)
        if data is None:
            return JSONResponse(
                status_code=413,
                content={
                    "detail": (
                        f"Upload exceeds the {limit_mb} MB limit. Raise MAX_UPLOAD_MB "
                        "in .env and restart if you really need bigger files."
                    )
                },
            )
        result = engine.ingest_file(file.filename or "upload.txt", data)
        return _document_response(result)

    @app.post("/documents/text", status_code=201)
    def ingest_text(request: Request, body: TextIngestRequest):
        result = _engine(request).ingest_text(body.file_name, body.text)
        return _document_response(result)

    @app.get("/documents")
    def list_documents(request: Request):
        return {"documents": _engine(request).list_documents()}

    @app.delete("/documents/{document_id}")
    def delete_document(request: Request, document_id: int):
        if not _engine(request).delete_document(document_id):
            raise UnknownDocumentError(f"No document with id {document_id}")
        return {"deleted": document_id}

    @app.post("/query")
    def query(request: Request, body: QueryRequest):
        return _engine(request).query(
            body.question,
            document_id=body.document_id,
            max_tokens=body.max_tokens,
            temperature=body.temperature,
            history=[turn.model_dump() for turn in body.history] if body.history else None,
            json_schema=body.json_schema,
        )

    @app.post("/verify")
    def verify(request: Request, body: VerifyRequest):
        return _engine(request).verify_claim(
            body.claim, document_id=body.document_id, max_tokens=body.max_tokens
        )

    @app.post("/documents/{document_id}/calibrate")
    def calibrate(request: Request, document_id: int, body: CalibrateRequest):
        engine = _engine(request)
        cap = engine.settings.calibrate_max_items
        if len(body.qa) > cap:
            # Enforced here (not only via Pydantic) so the message can name the knob,
            # mirroring the upload-cap 413 pattern.
            return JSONResponse(
                status_code=422,
                content={
                    "detail": (
                        f"Calibration battery has {len(body.qa)} items but the cap is {cap}. "
                        "Raise CALIBRATE_MAX_ITEMS or split the battery."
                    )
                },
            )
        return engine.calibrate(
            document_id, [item.model_dump() for item in body.qa],
            strict=body.strict, max_tokens=body.max_tokens,
        )

    @app.get("/stats")
    def stats(request: Request):
        return _engine(request).usage_stats()

    @app.post("/maintenance")
    def maintenance(request: Request):
        return _engine(request).maintenance()

    return app


def _document_response(result: dict) -> dict:
    # Never echo full document content back through the API.
    return {key: value for key, value in result.items() if key != "content"}


def _read_limited(stream, limit_bytes: int) -> bytes | None:
    """Read an upload in 1 MB chunks, stopping at the cap.

    Returns the full body, or None the moment the running total exceeds
    limit_bytes — the oversized remainder is never buffered."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = stream.read(1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > limit_bytes:
            return None
        chunks.append(chunk)


app = create_app()
