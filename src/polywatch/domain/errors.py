"""Error domain. Semuanya bisa dipetakan ke pesan singkat untuk pengguna."""

from __future__ import annotations


class DomainError(Exception):
    """Kesalahan aturan bisnis."""


class InvalidMarketSlugError(DomainError):
    """Slug market tidak sesuai format Polymarket Up/Down."""

    def __init__(self, raw: str) -> None:
        super().__init__(f"slug market tidak valid: {raw!r}")
        self.raw = raw


class InvalidAssetError(DomainError):
    """Simbol aset tidak valid."""

    def __init__(self, raw: str) -> None:
        super().__init__(f"simbol aset tidak valid: {raw!r}")
        self.raw = raw


class InvalidWalletError(DomainError):
    """Alamat wallet bukan alamat EVM yang valid."""

    def __init__(self, raw: str) -> None:
        super().__init__(f"alamat wallet tidak valid: {raw!r}")
        self.raw = raw
