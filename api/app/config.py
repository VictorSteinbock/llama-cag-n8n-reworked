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

    @property
    def slot_ctx_size(self) -> int:
        return self.llama_ctx_size // max(self.cag_slots, 1)

    @property
    def db_conninfo(self) -> str:
        return (
            f"host={self.db_host} port={self.db_port} dbname={self.db_name} "
            f"user={self.db_user} password={self.db_password}"
        )
