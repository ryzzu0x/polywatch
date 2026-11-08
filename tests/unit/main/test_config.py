"""Unit test konfigurasi env (T-003)."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from polywatch.main.config import Settings

_VALID_ENV = {
    "BOT_TOKEN": "123456:token-test",
    "OWNER_CHAT_ID": "513804114",
    "DATABASE_URL": "postgresql+asyncpg://user:pass@localhost:5432/polywatch_db",
}


def _settings(**env: str) -> Settings:
    return Settings.model_validate({**_VALID_ENV, **env})


def test_env_valid_terbaca() -> None:
    settings = _settings()
    assert settings.owner_chat_id == 513804114
    assert settings.bot_token.get_secret_value() == "123456:token-test"
    assert settings.log_level == "INFO"


def test_env_kosong_pesan_field_jelas(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("BOT_TOKEN", "OWNER_CHAT_ID", "DATABASE_URL"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)
    fields = {str(error["loc"][0]) for error in excinfo.value.errors() if error["loc"]}
    assert {"BOT_TOKEN", "OWNER_CHAT_ID", "DATABASE_URL"} <= fields


def test_dsn_non_asyncpg_ditolak() -> None:
    for bad in (
        "postgresql://user:pass@localhost:5432/db",
        "postgres://user:pass@localhost:5432/db",
        "sqlite+aiosqlite:///db.sqlite3",
    ):
        with pytest.raises(ValidationError) as excinfo:
            _settings(DATABASE_URL=bad)
        assert "asyncpg" in str(excinfo.value)


def test_owner_chat_id_nol_ditolak() -> None:
    with pytest.raises(ValidationError) as excinfo:
        _settings(OWNER_CHAT_ID="0")
    assert "OWNER_CHAT_ID" in str(excinfo.value)


def test_log_level_invalid_ditolak() -> None:
    with pytest.raises(ValidationError):
        _settings(LOG_LEVEL="CHATTY")


def test_secret_tidak_tercetak_repr() -> None:
    settings = _settings()
    assert "123456:token-test" not in repr(settings)
    assert "pass@localhost" not in repr(settings)
    assert settings.bot_token == SecretStr("123456:token-test")
