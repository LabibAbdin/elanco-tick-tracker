"""Application settings, loaded from environment variables (or a .env file).

Every tunable that affects resilience or data handling lives here so it can be
changed per environment without touching code.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "UK Tick Tracker API"
    environment: str = "development"
    log_level: str = "INFO"
    log_json: bool = True

    # --- Storage -----------------------------------------------------------
    database_url: str = f"sqlite:///{PROJECT_ROOT / 'data' / 'ticks.db'}"

    # --- Upstream source ---------------------------------------------------
    upstream_base_url: str = "https://dev-task.elancoapps.com/data"
    http_connect_timeout_s: float = 5.0
    http_read_timeout_s: float = 20.0
    http_max_attempts: int = Field(4, ge=1, le=10)
    http_backoff_base_s: float = 0.5
    http_backoff_cap_s: float = 8.0
    http_max_payload_mb: int = 200
    partition_concurrency: int = Field(4, ge=1, le=32)

    # Circuit breaker: after N consecutive failures stop calling upstream for a while.
    circuit_failure_threshold: int = 5
    circuit_recovery_s: float = 60.0

    # --- Ingestion ----------------------------------------------------------
    sync_on_startup: bool = True
    sync_interval_minutes: int = 60  # 0 disables the periodic sync
    batch_size: int = Field(500, ge=1, le=10_000)
    snapshot_path: Path = PROJECT_ROOT / "data" / "snapshot" / "tick_sightings_snapshot.json"
    # Two reports of the same species in the same city within this window are treated as
    # probable duplicates (e.g. a double-submitted crowdsourced form).
    duplicate_window_s: int = 300
    min_valid_year: int = 1990

    # --- API ----------------------------------------------------------------
    cors_origins: str = "*"
    default_page_size: int = 50
    max_page_size: int = 500
    admin_api_key: str | None = None  # when set, POST /ingestion/sync requires X-API-Key
    cache_size: int = 512

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
