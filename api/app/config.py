from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """All configuration comes from environment variables (set in docker-compose.yml)."""

    llama_server_url: str = "http://llama-server:8080"
    # Mirrors the --ctx-size llama-server was started with; used to reject
    # documents that cannot fit before wasting minutes of prompt processing.
    llama_ctx_size: int = 65536
    # Mirrors llama-server's --parallel: how many documents can be "hot"
    # (KV state resident in RAM) at once. llama-server divides the total
    # context evenly, so each slot gets llama_ctx_size / cag_slots tokens.
    cag_slots: int = 1
    # Context head-room kept free for the question + generated answer.
    answer_reserve_tokens: int = 1024
    # Additional head-room for what surrounds the raw document text in the
    # prompt: the SYSTEM_TEMPLATE wrapper, chat-template framing, and the
    # question turn. Subtracted from the per-slot document limit alongside
    # answer_reserve_tokens.
    prompt_overhead_tokens: int = 96
    # Orphaned cache files younger than this are skipped (reported, not
    # deleted) by maintenance — a file with no DB row yet may simply be an
    # ingest or self-heal still in flight.
    maintenance_grace_s: int = 3600
    # Server-side cap for POST /documents uploads. Mirrors the MCP client's
    # 50 MB client-side guard (MAX_FILE_BYTES in mcp/cag_mcp/server.py) so both
    # ends of that path agree; the HTTP route reads uploads in chunks and stops
    # at this cap instead of buffering an unbounded body.
    max_upload_mb: int = 50

    # Same volume llama-server writes slot files into (--slot-save-path).
    cache_dir: Path = Path("/caches")

    db_host: str = "db"
    db_port: int = 5432
    db_name: str = "llamacag"
    db_user: str = "llamacag"
    db_password: str = ""

    # CPU inference over a 30k-token document can legitimately take many
    # minutes; these are ceilings, not expectations.
    warm_timeout_s: float = 3600.0
    query_timeout_s: float = 600.0
    health_timeout_s: float = 5.0

    default_max_answer_tokens: int = 1024
    default_temperature: float = 0.2

    # Cost-savings estimate dial (GET /stats). A cloud provider's per-1k *input*-token
    # price; savings ≈ tokens_reused/1000 × this. 0.0 (default) hides the money line.
    cloud_price_per_1k_input: float = 0.0

    # Paraphrase tolerance for POST /verify's mechanical quote check: the minimum
    # difflib ratio at which a non-exact quote still counts as grounded. Higher =
    # stricter. Behavioral (not geometry), but plumbed through docker-compose.yml's
    # cag-api env allowlist + .env.example so setting it actually reaches the container.
    quote_match_threshold: float = 0.9

    @property
    def slot_ctx_size(self) -> int:
        return self.llama_ctx_size // max(self.cag_slots, 1)

    @property
    def db_conninfo(self) -> str:
        return (
            f"host={self.db_host} port={self.db_port} dbname={self.db_name} "
            f"user={self.db_user} password={self.db_password}"
        )
