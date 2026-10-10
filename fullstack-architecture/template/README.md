# Template: FastAPI + HTMX + Alpine.js + MariaDB

Project inti untuk skill `fullstack-architecture`: core, auth (session/CSRF/role/rate limit login), dan contoh
domain `items` (CRUD). Salin pola `items` untuk domain baru. Bila `items` dihapus: set `HOME_PATH` (halaman
setelah login dan `/`) ke domain Anda, hapus `tests/test_items.py` dan `static/js/alpine/items.js`, lalu buat ulang
migrasi awal. Test auth dan platform tidak bergantung pada `items`.
Modul opsional ada di `<skill>/addons/`: `jobs` (`references/jobs.md`) dan `api-tokens` (`references/auth.md` §6).

Versi acuan (2026-10-10): Python 3.13, FastAPI 0.143, Starlette 1.7, SQLAlchemy 2.1.4, asyncmy 0.2.16,
Alembic 1.20, Pydantic 2.14, htmx 2.0.4, Alpine CSP 3.14.8.
**Status verifikasi**: setelah pemisahan addon dan perbaikan review 2026-10-10 (lockout per pasangan username/IP,
batas mutlak session, `/readyz` di luar pool, worker tahan error claim, `HOME_PATH`, handler `RequestValidationError`),
lalu perbaikan go-live 2026-10-10 (`/readyz` tidak publik + maks. satu probe per detik, keepalive nginx < uvicorn,
database wajib utf8mb4, username terlalu panjang → 400 bukan 500, `change_password`, CLI `app.auth.cli`), lalu
`uv.lock` teruji, `static_url` ber-hash, `Cache-Control: no-store` default, log JSON per request, rotasi log,
filter `localtime`, checksum vendor JS, dan CI.
Review 2026-10-10 di MariaDB 11.4: menemukan dan memperbaiki migrasi yang tidak mencatat `alembic_version`
(`migrations/env.py`; deploy kedua gagal "Table already exists") dan menambah guard nama database test `*_test`.
Test suite lulus dua kali berturut-turut di database yang sama: 43 test inti, 58 dengan kedua addon; ruff bersih.
Review 2026-10-10 (lanjutan) di MariaDB 11.4.8: request ID dari nginx + validasi format, kebijakan password & role
di service, `change_role` aman untuk user yang tidak ada, rate limit IPv6 per /64, `login_failures` menyimpan
`user_id` (bukan teks username), `historyCacheSize: 0`, CLI token addon api-tokens, watchdog worker addon jobs,
CI dengan action ber-SHA + `pip-audit` + Dependabot. Hasil: 51 test inti, 70 dengan kedua addon, lulus dua kali
berturut-turut; ruff bersih; uji mutasi untuk setiap perbaikan tertangkap.
Uji mutasi `references/testing.md` §4 diulang sebagian (SKIP LOCKED, filter `owner_id`, CSRF, semaphore argon2,
`max_statement_time`): semua tertangkap. Tetap jalankan test suite (di bawah) untuk konfigurasi yang dipakai sebelum rilis.

## Mulai project baru

```bash
cp -r <skill>/template nama-project && cd nama-project
uv sync                              # uv.lock ikut tersalin = versi acuan di atas; commit bersama project
./scripts/fetch_vendor.sh            # htmx + Alpine (build CSP) ke app/static/js/vendor/; commit hasilnya
cp .env.example .env                 # isi DATABASE_URL (MariaDB eksternal, database utf8mb4) & SECRET_KEY
uv run alembic upgrade head
uv run python -m app.auth.cli create-user admin --role admin   # admin pertama; password dari prompt
```

Naik versi dependency: `uv lock --upgrade`, jalankan test suite di MariaDB, baru commit `uv.lock`.
CI siap pakai di `.github/workflows/ci.yml` (lint, test di MariaDB asli, build image).

Hapus migrasi contoh (`migrations/versions/*`) bila skema awal berbeda, lalu
`uv run alembic revision --autogenerate -m "initial schema"` dan **review hasilnya** (`references/database.md` §3).

## Menjalankan

```bash
# Development (static di-serve FastAPI karena DEBUG=true)
DEBUG=true COOKIE_SECURE=false uv run uvicorn app.main:app --reload

# Production-like (MariaDB eksternal dari DATABASE_URL)
docker compose build
docker compose run --rm web alembic upgrade head   # migrasi: sekali per deploy, sebelum up
docker compose run --rm web python -m app.auth.cli create-user admin --role admin   # hanya deploy pertama
docker compose up -d                               # web → nginx
# Terjadwal tiap jam (cron/systemd timer): hapus session kedaluwarsa & catatan login lama
docker compose run --rm web python -m app.auth.cleanup
```

## Test

Butuh MariaDB asli (bukan SQLite). Database test dihapus-isi oleh test suite, jadi namanya wajib berakhiran
`_test` (conftest menolak nama lain).

```bash
docker run -d --name test-mariadb -p 127.0.0.1:33061:3306 \
  -e MARIADB_ROOT_PASSWORD=root -e MARIADB_DATABASE=app_test \
  -e MARIADB_USER=app -e MARIADB_PASSWORD=app mariadb:11.4 \
  --character-set-server=utf8mb4 --collation-server=utf8mb4_unicode_ci
TEST_DATABASE_URL="mysql+asyncmy://app:app@127.0.0.1:33061/app_test" uv run pytest
```

## Peta file

| Topik | File |
|---|---|
| Engine, `DbSession`, `db_now_plus`, `delete_in_batches` | `app/core/database.py` |
| Auto-discovery model/task/router/template | `app/core/discovery.py` |
| Render full page vs fragment, `static_url`, filter `localtime` | `app/core/templating.py` |
| Error handler sadar-HTMX | `app/core/errors.py` |
| Security headers, `Cache-Control`, request ID, log JSON per request | `app/core/middleware.py`, `app/core/logging.py` |
| Health check | `app/core/health.py` |
| Session login, CSRF, role, rate limit, cleanup | `app/auth/` |
| Buat user / reset password dari terminal | `app/auth/cli.py` |
| Contoh domain lengkap | `app/items/` |
| Deploy | `Dockerfile`, `docker-compose.yml`, `deploy/nginx.conf` |
| CI | `.github/workflows/ci.yml` |
