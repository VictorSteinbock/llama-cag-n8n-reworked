"""FastMCP server exposing the local CAG stack as a document-memory tool.

An MCP client (Claude Code, Claude Desktop, any agent) talks to this server over
stdio; the server calls the local cag-api over HTTP. The point is that the
cloud agent's context never carries the document: it asks a question through
``ask_document`` and gets back only the answer. The whole document lives in
llama-server's KV cache on this machine, read exactly once.

The tool docstrings are the agent-facing contract — they tell a coding agent
exactly when to reach for each tool.
"""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .client import CagApiError, CagApiUnreachable, CagClient

CAG_API_URL = os.environ.get("CAG_API_URL", "http://localhost:8000")

# Largest file we will read off disk and stream to cag-api. cag-api enforces
# its own token-based limit and answers 413; this is only a client-side guard
# against slurping a huge blob into memory before the server ever sees it.
MAX_FILE_BYTES = 50 * 1024 * 1024

_STACK_DOWN_HINT = (
    "The local CAG stack does not appear to be running (could not reach cag-api "
    f"at {CAG_API_URL}). Start it with `python llamacag.py start` in the stack "
    "repository, wait for `python llamacag.py status` to report healthy, then retry."
)

mcp = FastMCP(name="cag")


def _client() -> CagClient:
    return CagClient(CAG_API_URL)


def _provenance(result: dict) -> str:
    """One compact line describing where the answer came from and its cost.

    Example: ``[doc 7 manual.pdf · cache: memory · evaluated 43 of 28,443
    prompt tokens · 640 ms]``. The two token numbers are the question tokens
    actually evaluated vs. the full prompt (question + the document reused from
    the KV cache) — the gap between them is the whole value of this stack.
    """
    doc = result.get("document") or {}
    timings = result.get("timings") or {}
    evaluated = timings.get("prompt_tokens_evaluated")
    from_cache = timings.get("prompt_tokens_from_cache")
    total = None
    if isinstance(evaluated, int) and isinstance(from_cache, int):
        total = evaluated + from_cache

    parts = [f"doc {doc.get('id', '?')} {doc.get('file_name', '?')}"]
    parts.append(f"cache: {timings.get('cache_source', 'unknown')}")
    if isinstance(evaluated, int) and isinstance(total, int):
        parts.append(f"evaluated {evaluated:,} of {total:,} prompt tokens")
    elif isinstance(evaluated, int):
        parts.append(f"evaluated {evaluated:,} prompt tokens")
    duration = result.get("duration_ms")
    if isinstance(duration, int):
        parts.append(f"{duration:,} ms")
    return "[" + " · ".join(parts) + "]"


def _render_verdict(result: dict) -> str:
    """Agent-facing rendering of a /verify result: the verdict, the quote, and —
    the point of the endpoint — whether that quote is mechanically grounded in
    the source, plus any scope condition, then the same provenance line."""
    verdict = result.get("verdict", "?")
    quote = result.get("quote") or ""
    conditions = result.get("conditions") or ""
    grounded = result.get("quote_grounded")
    method = result.get("grounding_method", "?")
    ratio = result.get("match_ratio")

    if grounded is True:
        grounded_line = f"quote_grounded: yes — quote found in source ({method}, ratio {ratio})"
    elif grounded is False:
        grounded_line = (
            f"quote_grounded: NO — quote is NOT in the source, likely fabricated "
            f"({method}, ratio {ratio})"
        )
    else:
        grounded_line = "quote_grounded: n/a — no quote to check (absent verdict)"

    lines = [f"verdict: {verdict}"]
    if quote:
        lines.append(f'quote: "{quote}"')
    lines.append(grounded_line)
    if conditions:
        lines.append(f"conditions: {conditions}")
    lines.append("")
    lines.append(_provenance(result))
    return "\n".join(lines)


@mcp.tool()
def list_documents() -> str:
    """List the documents the local CAG stack currently knows about.

    Use this to discover what is already ingested before calling ask_document,
    or to get a document's id so you can target it. Returns one line per
    document with its id, file_name, status, token count, and when it was last
    used. Only documents with status ``cached`` are queryable with
    ask_document; a ``pending`` document is still warming and a ``failed`` one
    needs re-ingesting. If the list is empty, ingest something first with
    ingest_text or ingest_file.
    """
    try:
        with _client() as client:
            docs = client.list_documents()
    except CagApiUnreachable:
        return _STACK_DOWN_HINT
    except CagApiError as exc:
        return f"cag-api error {exc.status_code}: {exc.detail}"

    if not docs:
        return "No documents ingested yet. Use ingest_text or ingest_file first."

    lines = ["id  status   n_tokens  last_used_at         file_name"]
    for doc in docs:
        n_tokens = doc.get("n_tokens")
        lines.append(
            f"{_col(doc.get('id'), 3)} {_col(doc.get('status'), 8)} "
            f"{_col('' if n_tokens is None else f'{n_tokens:,}', 9)} "
            f"{_col(doc.get('last_used_at') or '-', 20)} {doc.get('file_name', '?')}"
        )
    return "\n".join(lines)


@mcp.tool()
def ask_document(
    question: str,
    document_id: int | None = None,
    max_tokens: int = 1024,
    json_schema: dict | None = None,
) -> str:
    """Ask a question about a document held in the local CAG stack.

    This is the reason the server exists: the document never enters your
    context. Instead of pasting a large spec/manual/contract into the
    conversation and paying to re-read it on every turn, call this tool — only
    your question and the model's answer cross the boundary. The document was
    read once into an on-disk KV cache and every question after that evaluates
    just the question tokens, so answers are fast and cost nothing per query.

    Prefer this over pasting document text whenever the source is already
    ingested (see list_documents). Pass ``document_id`` to target a specific
    document; omit it to ask the most recently cached one. ``max_tokens`` caps
    the answer length. The answer is returned followed by a single provenance
    line showing the document, cache source, tokens evaluated, and latency.

    Pass ``json_schema`` (a JSON Schema object) to constrain the answer: the
    reply is then guaranteed to be valid JSON matching that schema, so you can
    parse it directly with no post-processing. This is ideal for verification
    verdicts — e.g. a schema for ``{claim, verdict: supported|absent|
    contradicted, quote}`` turns a grounding check into a machine-readable
    result. It constrains sampling only; the cached document prefix is
    untouched, so the query stays as cheap as any other.
    """
    try:
        with _client() as client:
            result = client.query(
                question,
                document_id=document_id,
                max_tokens=max_tokens,
                json_schema=json_schema,
            )
    except CagApiUnreachable:
        return _STACK_DOWN_HINT
    except CagApiError as exc:
        if exc.status_code == 409:
            return (
                "No document is cached yet, so there is nothing to query. Ingest a "
                "document first with ingest_file or ingest_text, then ask again. "
                f"(cag-api said: {exc.detail})"
            )
        if exc.status_code == 404:
            return (
                f"No document with id {document_id} exists. Call list_documents to see "
                f"valid ids. (cag-api said: {exc.detail})"
            )
        return f"cag-api error {exc.status_code}: {exc.detail}"

    answer = result.get("answer", "")
    return f"{answer}\n\n{_provenance(result)}"


@mcp.tool()
def verify(claim: str, document_id: int | None = None) -> str:
    """Verify a claim against a document held in the local CAG stack.

    Use this to fact-check a statement — your own draft, another model's output,
    a user's assertion — against a pinned source of truth before you rely on it.
    The stack answers with a strict verdict (``supported``, ``contradicted``, or
    ``absent``) AND **mechanically** checks that the quote it cites actually
    occurs in the source document, reporting ``quote_grounded``. That catches a
    fabricated citation with no extra model call — the single most useful signal
    here: a ``supported`` verdict whose ``quote_grounded`` is ``no`` is not
    trustworthy.

    Pass ``document_id`` to target a specific document; omit it to verify against
    the most recently cached one. Returns the verdict, the cited quote, whether
    that quote is grounded, any scope ``conditions`` the document places on the
    claim (e.g. "only if defective"), and a provenance line.

    Limits worth knowing: grounding hardens ``supported``/``contradicted`` (there
    is a passage to check) but cannot harden ``absent`` (``quote_grounded`` is
    ``n/a``), and it verifies the quote's *existence*, not the claim's
    *entailment* — the model can still misread real evidence. Treat it as a
    fail-safe gate: trust ``supported`` + grounded; route everything else to a
    human.
    """
    try:
        with _client() as client:
            result = client.verify(claim, document_id=document_id)
    except CagApiUnreachable:
        return _STACK_DOWN_HINT
    except CagApiError as exc:
        if exc.status_code == 409:
            return (
                "No document is cached yet, so there is nothing to verify against. Ingest a "
                "document first with ingest_file or ingest_text, then verify. "
                f"(cag-api said: {exc.detail})"
            )
        if exc.status_code == 404:
            return (
                f"No document with id {document_id} exists. Call list_documents to see "
                f"valid ids. (cag-api said: {exc.detail})"
            )
        return f"cag-api error {exc.status_code}: {exc.detail}"

    return _render_verdict(result)


@mcp.tool()
def ingest_text(file_name: str, text: str) -> str:
    """Ingest raw text into the local CAG stack so it can be queried later.

    Use this to make a block of text — notes, a pasted spec, generated content —
    queryable with ask_document without keeping it in your context afterwards.
    ``file_name`` is a label (give it a real-looking name like ``spec.md`` so it
    is easy to identify later). The text is read once into an on-disk KV cache;
    that first warm can take a while for a large document, but every later
    question is cheap. Returns the assigned document id, status, and token
    count. If the text is too large to fit the model's context window, cag-api
    rejects it and this tool surfaces that message verbatim so you know the
    measured token count and which limit to raise.
    """
    try:
        with _client() as client:
            result = client.ingest_text(file_name, text)
    except CagApiUnreachable:
        return _STACK_DOWN_HINT
    except CagApiError as exc:
        # 413 (too large) and any other 4xx: the server's detail is the useful
        # part — surface it verbatim.
        return f"cag-api error {exc.status_code}: {exc.detail}"

    return _ingest_summary(result)


@mcp.tool()
def ingest_file(path: str) -> str:
    """Ingest a local file into the CAG stack so it can be queried later.

    Point this at a document on this machine (``.txt`` ``.md`` ``.html`` or a
    text-based ``.pdf``); its contents are read once into an on-disk KV cache
    and become queryable with ask_document, without the file ever entering your
    context. Use this instead of reading the file yourself and pasting it —
    that would spend context and money you do not need to spend. Returns the
    assigned document id, status, and token count. The file must exist and be
    under 50 MB (checked here); cag-api additionally rejects unsupported types
    (415) or documents too large for the context window (413), and this tool
    surfaces those messages verbatim.
    """
    file_path = Path(path).expanduser()
    if not file_path.exists():
        return f"No file exists at {file_path}. Provide an absolute path to a local document."
    if not file_path.is_file():
        return f"{file_path} is not a file (a directory?). Provide a path to a single document."
    size = file_path.stat().st_size
    if size == 0:
        return f"{file_path} is empty — nothing to ingest."
    if size > MAX_FILE_BYTES:
        return (
            f"{file_path} is {size:,} bytes, larger than the {MAX_FILE_BYTES:,}-byte "
            "client limit. Split the file or ingest a smaller document."
        )

    data = file_path.read_bytes()
    content_type, _ = mimetypes.guess_type(file_path.name)
    try:
        with _client() as client:
            result = client.ingest_file(file_path.name, data, content_type=content_type)
    except CagApiUnreachable:
        return _STACK_DOWN_HINT
    except CagApiError as exc:
        # 413 (too large) / 415 (unsupported type) / others: surface verbatim.
        return f"cag-api error {exc.status_code}: {exc.detail}"

    return _ingest_summary(result)


def _ingest_summary(result: dict) -> str:
    doc_id = result.get("id", "?")
    status = result.get("status", "?")
    n_tokens = result.get("n_tokens")
    tokens = "unknown" if n_tokens is None else f"{n_tokens:,}"
    file_name = result.get("file_name", "?")
    dedup = ""
    if result.get("deduplicated"):
        dedup = " (deduplicated — identical content was already ingested)"
    return (
        f"Ingested '{file_name}' as document {doc_id}: status={status}, "
        f"{tokens} tokens.{dedup} Query it with ask_document."
    )


def _col(value: object, width: int) -> str:
    return str("" if value is None else value).ljust(width)


def main() -> None:
    mcp.run("stdio")
