"""Konfigurasi dari environment. Gagal cepat bila wajib tidak lengkap."""

from __future__ import annotations

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

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
    discover_interval_s: float = Field(default=30.0, alias="DISCOVER_INTERVAL_S")
    result_interval_s: float = Field(default=30.0, alias="RESULT_INTERVAL_S")
    heartbeat_interval_s: float = Field(default=60.0, alias="HEARTBEAT_INTERVAL_S")

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
