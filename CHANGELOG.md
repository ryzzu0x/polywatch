# Changelog

Format mengikuti [Keep a Changelog](https://keepachangelog.com/id/1.1.0/),
versi mengikuti [Semantic Versioning](https://semver.org/lang/id/).

## [Unreleased]

### Added
- Fondasi proyek: struktur Clean Architecture (`domain`, `application`, `infrastructure`,
  `presentation`, `main`), konfigurasi env tervalidasi, logging JSON, entrypoint CLI.
- Domain awal: `MarketSlug`, `Timeframe`, `Asset`, `Outcome`, `WalletStats`.
- Port/interface: sumber market/trade/price, repository, notifier, clock.
- Tooling: ruff, mypy strict, import-linter (aturan layer), pytest, CI GitHub Actions.

## [0.1.0] - 2026-11-08

### Added
- Inisialisasi repository dan salinan dokumen planning di `docs/`.
