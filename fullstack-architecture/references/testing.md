# Strategi Testing

File acuan: `template/tests/` (inti) dan `addons/*/tests/` (ikut tersalin saat addon dipasang). Cara menjalankan ada di `template/README.md`.

## 1. MariaDB asli, bukan SQLite

Jaminan terpenting arsitektur ini hanya bisa dibuktikan di MariaDB: `max_statement_time`, `time_zone`, perilaku lock, migrasi, dan `SKIP LOCKED` (addon jobs). Jalankan MariaDB di Docker dan set `TEST_DATABASE_URL`.

## 2. Pola fixture (`tests/conftest.py`)

| Fixture | Alasan |
|---|---|
| Nama database `TEST_DATABASE_URL` wajib berakhiran `_test` (selain itu conftest menolak jalan) | Test suite men-downgrade semua migrasi dan `TRUNCATE` semua tabel: URL yang salah tunjuk ke DB dev/production menghapus datanya. |
| Set env (`DATABASE_URL`, `COOKIE_SECURE=false`) **sebelum** import `app` | `settings` dibaca saat import. |
| `alembic downgrade base` → `upgrade head` sekali per sesi | Skema test = skema production, dan migrasi teruji dua arah. |
| `TRUNCATE` semua tabel setelah tiap test | Kode yang diuji meng-commit sendiri (service pemilik transaksi), jadi pola "rollback per test" tidak berlaku. |
| Satu event loop untuk seluruh sesi (`asyncio_default_*_loop_scope = "session"`) | Koneksi engine async terikat ke loop tempat koneksi itu dibuat. |
| `httpx.AsyncClient(transport=ASGITransport(app))` | Request melewati middleware, dependency, dan error handler asli. |
| Helper `login()` lewat form asli | Ikut menguji double-submit CSRF dan pembuatan session. |

CI: `template/.github/workflows/ci.yml` menjalankan lint, test di MariaDB 11.4 (utf8mb4, UTC), dan build image pada setiap push/PR.

## 3. Yang wajib diuji (inti)

| Area | Test |
|---|---|
| HTMX | Full page vs fragment (row hasil create memuat `created_at` tanpa lazy load), `Vary`, history restore, script Alpine hanya di halaman pemakainya, 422 + `HX-Retarget`, `HX-Trigger`, 401 → `HX-Redirect`, error → toast, query tidak valid → toast (bukan JSON mentah; `/api/` tetap JSON) |
| Auth | Session disimpan sebagai hash, CSRF wajib, rate limit login (termasuk 15 percobaan paralel: ≤ `LOGIN_MAX_FAILURES` yang diverifikasi; lockout per (username, IP) tidak mengunci pemilik akun di IP lain; batas per IP lintas username), throttle `last_seen_at`, perpanjangan session tidak melewati `SESSION_MAX_AGE` dan `max_age` cookie = `SESSION_MAX_AGE`, perubahan role mencabut session, argon2 paralel ≤ `PASSWORD_HASH_CONCURRENCY`, cleanup menghapus session kedaluwarsa & catatan login lama, username terlalu panjang → 400 (bukan 500), ganti password mencabut session, CLI membuat admin & reset password. Test auth memakai route khusus test (`/__whoami`), tidak bergantung pada domain `items`. |
| Otorisasi | Endpoint admin menolak role biasa (403); user hanya melihat item miliknya, admin melihat semua |
| Platform | Security headers, request ID, exception tak tertangani → 500 tetap membawa CSP + `X-Request-ID` dan log-nya ber-`request_id` (browser & HTMX), `SECRET_KEY` < 32 karakter ditolak, `time_zone=+00:00`, `max_statement_time` menghentikan query lambat, migrasi mencatat versi di `alembic_version` (tanpa itu deploy berikutnya gagal "Table already exists"), health check, `/readyz` tetap 200 saat pool web penuh, `/readyz` paralel hanya memicu satu probe DB, database `utf8mb4` menyimpan emoji, `Cache-Control: no-store` default (tidak menimpa header endpoint), `static_url` ber-hash, filter `localtime`, satu log JSON `request` per request (tanpa health check), log uvicorn lewat handler JSON |

Addon: `jobs.md` §6 dan `auth.md` §6.

## 4. Pastikan test bisa gagal (uji mutasi)

Test yang selalu lulus belum tentu menguji sesuatu. Untuk jaminan inti, rusak kodenya dengan sengaja lalu pastikan test-nya gagal. Hasil terakhir (dijalankan **sebelum** pemisahan addon; jalankan ulang setelah perubahan besar):

| Mutasi | Akibat yang tertangkap test |
|---|---|
| Auth memakai `Depends(get_db)` (session request) | `InvalidRequestError: A transaction is already begun` saat service memanggil `db.begin()` |
| Hapus semaphore argon2 | Lebih dari `PASSWORD_HASH_CONCURRENCY` verifikasi berjalan paralel |
| Login menghitung dulu, mencatat kegagalan belakangan | Percobaan paralel melewati `LOGIN_MAX_FAILURES` |
| Hapus filter `owner_id` di `/items` | User melihat item milik user lain |
| Hapus `{% block scripts %}` di halaman items | Komponen Alpine tidak dimuat |
| 500 lewat `exception_handler(Exception)`, bukan `UnhandledErrorMiddleware` | Respons 500 tanpa CSP & `X-Request-ID`; log `unhandled_error` ber-`request_id: null` |
| `delete_in_batches` berhenti setelah satu batch | Backlog tersisa setelah satu putaran (test addon jobs) |
| Hapus `.with_for_update(skip_locked=True)` di `claim` (addon jobs) | Job yang sama dijalankan 3× oleh 3 worker |
| Hapus requeue di `shutdown()` (addon jobs) | Job tertinggal `running` sampai `locked_until` lewat |
| `claim_batch = 1` (addon jobs) | 57 claim untuk 200 job, bukan 20 |
| Abaikan hasil `GET_LOCK` (addon jobs) | Maintenance berjalan walau worker lain memegang lock |

## 5. Smoke test production-like

Sebelum rilis besar, jalankan migrasi lalu `docker compose up -d --build` (`template/README.md`) dan verifikasi lewat nginx:
- `/healthz` dan `/readyz` lewat nginx dari luar: 403;
- `/login` → login → `POST /items` dengan `HX-Request`: response membawa `HX-Trigger`;
- POST `/login` beruntun: nginx membalas 429;
- POST `/login` gagal dengan header `X-Forwarded-For: 203.0.113.9`: kolom `login_failures.ip` berisi IP asli client, **bukan** `203.0.113.9`;
- DB tidak terjangkau (mis. blokir akses ke host DB): `/readyz` 503, `/healthz` tetap 200;
- addon jobs: job diproses worker, dan `request_id` dari request web muncul di log worker.
