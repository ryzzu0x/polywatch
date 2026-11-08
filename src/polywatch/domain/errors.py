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


class InvalidMarketStatusTransitionError(DomainError):
    """Transisi status market tidak legal (mundur atau melompati tahap)."""

    def __init__(self, current: str, target: str) -> None:
        super().__init__(f"transisi status market ilegal: {current} -> {target}")
        self.current = current
        self.target = target


class InvalidOutcomePricesError(DomainError):
    """outcomePrices tidak berbentuk pasangan resolusi [\"1\",\"0\"] / [\"0\",\"1\"]."""

    def __init__(self, raw: object) -> None:
        super().__init__(f"outcomePrices tidak valid: {raw!r}")
        self.raw = raw


class InvalidResolutionStatusError(DomainError):
    """umaResolutionStatus memakai nilai yang tidak dikenal."""

    def __init__(self, raw: object) -> None:
        super().__init__(f"umaResolutionStatus tidak dikenal: {raw!r}")
        self.raw = raw
