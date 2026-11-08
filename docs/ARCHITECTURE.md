# ARCHITECTURE — polywatch

> Status: Draft v0.1

---

## 1. Ringkasan Teknis

| Aspek | Pilihan |
|---|---|
| Bahasa | Python 3.12 (asyncio) |
| Backend/Framework | aiogram 3 (bot Telegram) — hanya di presentation |
| Database | PostgreSQL 16 |
| Akses data | SQLAlchemy 2 (async, asyncpg) + Alembic — hanya di infrastructure |
| HTTP client | httpx (async) dengan retry/backoff |
| Frontend | Tidak ada (Telegram saja) |
| Auth | Bot hanya melayani `OWNER_CHAT_ID` di MVP |
| Test | pytest, pytest-asyncio, respx (mock HTTP), PostgreSQL nyata untuk integration |
| Lint/type | ruff, mypy, import-linter (boundary layer) |
| CI | GitHub Actions |
| Deploy | systemd di VPS, venv (uv), tanpa Docker |
| v2 (rencana) | eth-account (generate wallet), py-clob-client (order Polymarket), cryptography (Fernet/AES-GCM) |

Bentuk sistem: **modular monolith, satu proses** berisi scheduler/worker + bot. Cukup untuk ±2.700 market/hari dan mudah di-deploy di VPS kecil.

## 2. Diagram Layer (Aturan Ketergantungan) — Standar

```
presentation    (bot Telegram: command, formatter alert)
      │ memanggil
      ▼
application     (use case, port/interface, DTO)
      │ memakai
      ▼
domain          (Market, Trade, Position, WalletStats, ScoringPolicy)
      ▲
      │ mengimplementasikan port
infrastructure  (Gamma/Data/CLOB client, PostgreSQL repo, price source, clock, notifier)

main/ = composition root: merangkai semua dependency di satu tempat.
```

**Aturan:**
1. `domain` tidak meng-import apa pun dari layer lain maupun library framework.
2. `application` hanya bergantung pada `domain` dan port miliknya sendiri.
3. `infrastructure` dan `presentation` boleh bergantung ke `application`/`domain`, tidak sebaliknya.
4. Dijaga oleh `import-linter` di CI.

## 3. Struktur Folder

```
Code/
├── src/polywatch/
│   ├── domain/
│   │   ├── market.py          # Market, Timeframe, Asset, Outcome, MarketResult
│   │   ├── trade.py           # Trade, Side, Action
│   │   ├── position.py        # posisi bersih per wallet per market
│   │   └── scoring.py         # WalletStats, edge, skor, filter market maker
│   ├── application/
│   │   ├── ports/             # MarketSource, TradeSource, PriceSource, Repos, Notifier, Clock
│   │   ├── use_cases/         # discover_markets, ingest_trades, record_result,
│   │   │                      # recompute_scores, evaluate_alert, manage_settings
│   │   └── dto/
│   ├── infrastructure/
│   │   ├── polymarket/        # gamma_client, data_client, clob_client
│   │   ├── prices/            # sumber open/close (ditentukan spike T-005)
│   │   ├── db/                # models, repositories, alembic/
│   │   └── telegram_notifier.py
│   ├── presentation/
│   │   └── bot/               # handlers, keyboards, formatters
│   └── main/                  # config (env), container, scheduler, entrypoint
├── tests/{unit,integration,e2e}/
├── scripts/                   # deploy.sh, backup.sh, restore.sh
├── docs/                      # salinan Detail Project/
├── .env.example
├── pyproject.toml
├── README.md
└── CHANGELOG.md
```

## 4. Model Data

```
assets          (id, symbol UNIQUE, name, enabled_default)
watch_config    (id, user_id, asset_id FK, timeframe ['5m'|'15m'], enabled, UNIQUE(user_id, asset_id, timeframe))
markets         (id, condition_id UNIQUE, slug UNIQUE, asset_id FK, timeframe, start_at, end_at,
                 token_up, token_down, status ['upcoming'|'live'|'closed'|'resolved'])
market_results  (market_id PK/FK, open_price NUMERIC, close_price NUMERIC, result ['up'|'down'],
                 volume_usdc NUMERIC, price_source, resolved_at)
trades          (id, tx_hash, log_index, market_id FK, wallet, outcome ['up'|'down'],
                 action ['buy'|'sell'], price NUMERIC(6,4), size NUMERIC, usdc NUMERIC, traded_at,
                 UNIQUE(tx_hash, log_index, wallet))   -- idempoten
positions       (wallet, market_id, net_up, net_down, cost_up, cost_down, pnl, is_two_sided,
                 PRIMARY KEY(wallet, market_id))
wallet_stats    (wallet PK, markets_count, wins, win_rate, avg_entry, edge, pnl, roi, score,
                 is_market_maker, last_trade_at, updated_at)
watchlist       (user_id, wallet, PRIMARY KEY(user_id, wallet))
alert_settings  (user_id PK, min_score, min_usdc, cooldown_sec, enabled)
alerts_sent     (id, user_id, wallet, market_id, trade_id, sent_at, delay_ms)
users           (id, telegram_id UNIQUE, created_at)        -- MVP: hanya owner
-- v2:
user_wallets    (user_id PK/FK, address UNIQUE, enc_private_key BYTEA, key_version, created_at)
copy_rules      (user_id, wallet, size_mode, size_value, max_per_trade, max_daily, enabled)
orders          (id, user_id, source_trade_id, market_id, side, price, size, status, clob_order_id, error, created_at)
```

Catatan:
- Uang & harga pakai `NUMERIC`, tidak pernah float.
- Index: `trades(wallet, traded_at)`, `trades(market_id)`, `markets(start_at)`, `wallet_stats(score DESC)`.
- Semua tabel per-pengguna sudah punya `user_id` sejak MVP supaya v2 tidak perlu migrasi besar.
- `enc_private_key` (v2) dienkripsi dengan master key dari env; master key tidak pernah ada di DB/log.

## 5. Kontrak Antarmuka (Perintah Bot)

| Perintah | Fungsi | Auth |
|---|---|---|
| `/start` | Daftar & menu (MVP: owner saja) | owner |
| `/status` | Market live terakhir, jumlah market per status, trade 24 jam, status WS/RPC, versi | owner |
| `/markets` | Daftar aset × timeframe + tombol inline toggle on/off (callback query) | owner |
| `/results <aset> <5m\|15m> [n]` | Hasil market terakhir (open, close, Up/Down, volume, waktu; n ≤ 20) | owner |
| `/top [n]` | Wallet skor tertinggi | owner |
| `/wallet <alamat>` | Statistik wallet | owner |
| `/watch <alamat>` · `/unwatch <alamat>` | Kelola watchlist | owner |
| `/alerts` | Atur skor minimal, size minimal, cooldown, on/off | owner |
| v2: `/wallet_me`, `/deposit`, `/copy`, `/limits`, `/stop`, `/export_key` | Wallet & eksekusi | per pengguna |

Error ditampilkan ke pengguna sebagai pesan singkat; detail (kode + trace id) masuk log.

## 6. Alur Data Utama

```
[Scheduler tiap 30 dtk] discover_markets
   Gamma API (slug {aset}-updown-{tf}-{unix}) → upsert markets (upcoming/live)

[Worker tiap 2–3 dtk per market live] ingest_trades
   Data API /trades?market=<condition_id> (cursor waktu) → insert idempoten trades
   → update positions → evaluate_alert(trade)
        skor wallet ≥ ambang atau di watchlist, size ≥ minimal, bukan cooldown
        → ambil harga sekarang (CLOB) → Notifier.send(alert) → catat alerts_sent(delay_ms)

[Setelah end_at] record_result
   ambil open/close price (PriceSource) + result resmi + volume (Gamma) → market_results
   (retry tiap 30 dtk sampai resolved; > 15 mnt tanpa resolve → notifikasi error)

[Setelah result tersimpan] recompute_scores(wallet yang trade di market itu)
   positions → wallet_stats (edge, ROI, skor, flag market maker)
```

Kegagalan API: retry dengan exponential backoff + jitter; error berulang dikirim ke owner maksimal 1×/10 menit.

## 7. Integrasi Eksternal

> Diperbarui setelah spike T-005 (2026-11-08) — laporan: `docs/spikes/T-005-sumber-data.md`, keputusan di `DECISIONS.md` ADR-004/ADR-005.

- **Polymarket Gamma API** (`gamma-api.polymarket.com`) — daftar market (`GET /markets?closed=&order=endDate&ascending=&end_date_max|end_date_min=<ISO>`, `limit=100` per halaman), field kunci `conditionId`, `clobTokenIds` (string JSON), `slug` (pola `{aset}-updown-{5m|15m}-{unix_start}` terverifikasi), `outcomePrices`/`outcomes`, `umaResolutionStatus`, `volume` (terisi setelah resolve). Hasil resolusi: query `closed=true` (delay resolve setelah end ±6 menit; market live menghilang dari pencarian slug tanpa `closed=true`).
- **Polymarket Data API v2** (`data-api.polymarket.com/v2`) — trade per market: `GET /v2/trades?condition=<conditionId>&limit=500`, pagination **cursor-only** (`next_cursor`), field `proxy_wallet`, `side`, `token_id`, `condition_id`, `size`, `price`, `timestamp`, `outcome`, `outcome_index`, `transaction_hash`. Catatan: v1 sudah pensiun (410); latency trade→tersedia median ±164 s. Insert wajib idempoten (duplikat umum saat polling).
- **Polymarket CLOB API** — harga/orderbook saat ini (MVP); order (v2).
- **Sumber harga open/close (ADR-005)** — utama: `GET polymarket.com/api/crypto/crypto-price?symbol=<aset>&eventStartTime=<unix>` (`openPrice`/`closePrice` feed Chainlink selaras resolusi; hanya window aktif — tidak bisa backfill). Cadangan: Binance klines 1m (`api.binance.com/api/v3/klines`). Kolom `price_source` selalu disimpan.
- **Telegram Bot API** — via aiogram.
- **v2:** Polygon RPC (saldo, approval USDC), CLOB authenticated (L1/L2 key diturunkan dari wallet pengguna).

## 8. Keamanan (Baseline)

- Secret hanya lewat environment variable (`.env` tidak di-commit): `BOT_TOKEN`, `DATABASE_URL`, `OWNER_CHAT_ID`; v2: `WALLET_MASTER_KEY`.
- Bot menolak semua chat selain owner di MVP.
- Validasi input (alamat wallet `0x` + 40 hex, aset, timeframe) di presentation; aturan bisnis di domain.
- Rate limit per pengguna untuk perintah berat.
- **v2 (wajib sebelum rilis):** private key dienkripsi (AES-GCM) dengan master key di env; tidak pernah dicetak di log/pesan; ekspor key hanya via perintah eksplisit + konfirmasi, pesan dihapus otomatis; batas modal & kill switch per pengguna; audit log setiap order.

## 9. Observabilitas

- Log JSON terstruktur ke journald (`journalctl -u polywatch`).
- Metrik di log: delay ingest, delay alert, jumlah trade/menit, error API.
- `/status` di bot + heartbeat: jika ingest berhenti > 2 menit → notifikasi owner.

## 10. Deployment

```
Polymarket APIs ⇄ [VPS: polywatch.service (python, venv)] ⇄ PostgreSQL 16 (lokal VPS)
                                   ⇅
                            Telegram Bot API (long polling, tanpa port publik)
```

- Lokasi runtime di VPS: direktori project (`<PROJECT_DIR>/polywatch/`), runtime saja. Path host aktual tidak disimpan di repo — lihat catatan akses lokal.
- Alur deploy satu arah: lokal → commit → push GitHub → `git pull` di VPS → `uv sync` → `alembic upgrade head` → restart `polywatch.service`. Tidak ada edit kode langsung di VPS.
- Backup: `pg_dump` harian ke direktori backup di server (`<BACKUP_DIR>/polywatch/`, diatur lewat env deploy), retensi 14 hari.
- Rollback: `git checkout <tag sebelumnya>` + restore dump bila migrasi gagal; restart service.

## 11. Strategi Test

| Level | Cakupan | Alat |
|---|---|---|
| Unit | Domain (posisi, edge, skor, filter MM) dan use case dengan repo/port palsu | pytest |
| Integration | Repo PostgreSQL, client Polymarket (fixture respons nyata via respx) | pytest + PostgreSQL lokal |
| E2E | Core flow: market → trade → alert → resolve → skor, dengan sumber palsu & DB nyata | pytest-asyncio |

Quality gate CI: `ruff` → `mypy` → `import-linter` → `pytest`. Semua wajib hijau sebelum merge ke `main`.
