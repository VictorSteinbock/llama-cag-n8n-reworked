# mcp/ — the stack as an MCP tool (cag-mcp)

Expose the local CAG stack to any [MCP](https://modelcontextprotocol.io) client
— Claude Code, Claude Desktop, or any agent that speaks the protocol — as a
document-memory and verification tool. The point is context economy: the
document never enters the cloud model's context. It was read once into
llama-server's KV cache on this machine; only questions, answers, and verdicts
cross the boundary.

## The five tools

| Tool | What it does |
|---|---|
| `list_documents` | What's ingested, with ids, status, and token counts — call first to discover targets. |
| `ask_document` | Ask a question about a cached document; supports `json_schema` for guaranteed-parseable typed answers. Returns the answer plus a provenance line (cache source, tokens evaluated, latency). |
| `verify` | The grounding oracle as an agent tool: a verdict (`supported` / `contradicted` / `absent`), the cited quote, a **mechanical** `quote_grounded` byte-check that catches fabricated citations, the recall probe for `absent`, and any scope `conditions`. |
| `ingest_text` | Make a block of text queryable (notes, a pasted spec, generated content). |
| `ingest_file` | Ingest a local `.txt` / `.md` / `.html` / text-layer `.pdf` (50 MB client-side cap; cag-api enforces its own 413/415). |

The tool docstrings in [`cag_mcp/server.py`](cag_mcp/server.py) are the
agent-facing contract — they tell a coding agent exactly when to reach for
each tool, and errors come back as prose with a recovery path (stack down →
how to start it; no documents yet → ingest first).

## Quick start

```bash
pip install -e ./mcp            # in the stack repo
python llamacag.py start        # the stack must be running (and healthy)
claude mcp add cag -- python -m cag_mcp
```

Or in a project's `.mcp.json`:

```jsonc
{
  "mcpServers": {
    "cag": {
      "command": "python",
      "args": ["-m", "cag_mcp"],
      "env": { "CAG_API_URL": "http://localhost:8000" }
    }
  }
}
```

Configuration is one variable: `CAG_API_URL` (default `http://localhost:8000`),
pointing at cag-api. The server is stdio-only and holds no state of its own —
all HTTP knowledge lives in [`cag_mcp/client.py`](cag_mcp/client.py), mirroring
the error-mapping style of `api/app/llama.py`.

## Honest limits

- **Ingest is synchronous.** Warming a big document can take minutes and the
  tool call blocks for the duration ([ROADMAP](../docs/ROADMAP.md) F12, async
  ingest, is the planned fix).
- **`verify` proves a quote's *existence*, not the claim's *entailment*** —
  treat it as a fail-safe gate: trust `supported` + grounded, route the rest
  to a human. The full fail-safe policy table is in
  [`integrations/`](../integrations/README.md).
- Requires Python **3.11+** and a running stack; `pytest mcp` runs the test
  suite with no services needed.
