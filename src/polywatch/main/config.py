"""Konfigurasi dari environment. Gagal cepat bila wajib tidak lengkap."""

from __future__ import annotations

import tempfile
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_lock_file() -> str:
    return str(Path(tempfile.gettempdir()) / "polywatch.lock")


def _default_health_state_file() -> str:
    return str(Path(tempfile.gettempdir()) / "polywatch-health-state.json")


_ALLOWED_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


class Settings(BaseSettings):
    """Konfigurasi runtime polywatch."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    bot_token: SecretStr = Field(alias="BOT_TOKEN")
    owner_chat_id: int = Field(alias="OWNER_CHAT_ID")
    database_url: SecretStr = Field(alias="DATABASE_URL")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    #: Interval scheduler (detik) — siklus penuh discover/resolve/skor.
    #: T-029/ADR-016: default dinaikkan 30 → 60 dtk; siklus berbatas
    #: (batch + paralelisasi) target < 60 dtk, interval 60 dtk memberi
    #: ruang tanpa menumpuk siklus.
    discover_interval_s: float = Field(default=60.0, alias="DISCOVER_INTERVAL_S")
    result_interval_s: float = Field(default=60.0, alias="RESULT_INTERVAL_S")
    heartbeat_interval_s: float = Field(default=60.0, alias="HEARTBEAT_INTERVAL_S")

    #: Health monitor (T-019/T-027/T-029): ambang basi, rate limit, grace.
    #: T-027: kesegaran ingest diukur dari HEARTBEAT ingest (batch commit /
    #: siklus WS aktif), bukan trade market LIVE — ambang 600 dtk.
    #: T-029/ADR-016: ambang discover/result ADAPTIF — floor dari Settings,
    #: efektif = max(3x durasi siklus terukur, floor). Default floor 300 dtk.
    stale_ingest_s: float = Field(default=600.0, alias="STALE_INGEST_S")
    stale_discover_s: float = Field(default=300.0, alias="STALE_DISCOVER_S")
    stale_result_s: float = Field(default=300.0, alias="STALE_RESULT_S")
    health_rate_limit_s: float = Field(default=600.0, alias="HEALTH_RATE_LIMIT_S")
    health_grace_s: float = Field(default=300.0, alias="HEALTH_GRACE_S")
    health_check_interval_s: float = Field(default=30.0, alias="HEALTH_CHECK_INTERVAL_S")

    #: T-029/ADR-016: batas kerja per siklus + paralelisasi Gamma.
    #: Konkurensi maksimum request Gamma (semaphore; pacing tetap ≤5 req/s).
    gamma_max_concurrency: int = Field(default=5, alias="GAMMA_MAX_CONCURRENCY")
    #: Maksimum market CLOSED yang diproses record_result + sweep per siklus.
    result_batch_size: int = Field(default=10, alias="RESULT_BATCH_SIZE")

    #: State notifikasi health persisten antar-restart (T-027, ADR-014):
    #: file JSON kecil — restart berulang tidak memicu spam alarm.
    health_state_file: str = Field(
        default_factory=_default_health_state_file, alias="HEALTH_STATE_FILE"
    )

    #: Single-instance lock (T-027, ADR-014): flock eksklusif — dua proses
    #: bersamaan menyebabkan alert ganda + TelegramConflictError.
    lock_file: str = Field(default_factory=_default_lock_file, alias="LOCK_FILE")

    #: Aset default discovery market (T-024) — dipakai discover_markets.
    market_assets: tuple[str, ...] = Field(
        default=("btc", "eth", "sol", "xrp", "doge", "bnb", "hype", "zec", "xag", "xau"),
        alias="MARKET_ASSETS",
    )

    @field_validator("market_assets")
    @classmethod
    def _check_market_assets(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(a.strip().lower() for a in value if a and a.strip().isalnum())
        if not normalized:
            raise ValueError("MARKET_ASSETS tidak boleh kosong (contoh: btc,eth,sol)")
        return normalized

    polymarket_gamma_url: str = Field(
        default="https://gamma-api.polymarket.com", alias="POLYMARKET_GAMMA_URL"
    )
    polymarket_data_url: str = Field(
        default="https://data-api.polymarket.com", alias="POLYMARKET_DATA_URL"
    )
    polymarket_clob_url: str = Field(
        default="https://clob.polymarket.com", alias="POLYMARKET_CLOB_URL"
    )

    @field_validator("database_url")
    @classmethod
    def _check_driver(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if not raw.startswith("postgresql+asyncpg://"):
            raise ValueError(
                "DATABASE_URL harus memakai driver asyncpg: postgresql+asyncpg://user:pass@host/db"
            )
        return value

    @field_validator("log_level")
    @classmethod
    def _check_log_level(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in _ALLOWED_LOG_LEVELS:
            raise ValueError(f"LOG_LEVEL harus salah satu dari {sorted(_ALLOWED_LOG_LEVELS)}")
        return normalized

    @field_validator("owner_chat_id")
    @classmethod
    def _check_owner(cls, value: int) -> int:
        if value == 0:
            raise ValueError("OWNER_CHAT_ID belum diisi (0 tidak valid)")
        return value
