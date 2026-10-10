---
name: fullstack-architecture
description: Arsitektur fullstack FastAPI + HTMX + Alpine.js + Jinja2 + MariaDB (tanpa Redis) dengan template project dan test suite-nya. Gunakan saat membuat project baru, menambah domain, menulis atau me-review endpoint, service, transaksi DB async, template HTML/HTMX, auth/session/CSRF, migrasi, job queue, deployment, atau test di stack ini.
---

# Fullstack Architecture (FastAPI + HTMX + Alpine.js + MariaDB)

| Bagian | Isi |
|---|---|
| `SKILL.md` | Aturan inti. Selalu dibaca. |
| `template/` | Project inti beserta test suite: core, auth (session/CSRF/role/rate limit login, CLI user), contoh domain `items`. Kode di sini sumber kebenaran. Status verifikasi di `template/README.md`. |
| `addons/` | Modul opsional, disalin hanya saat dibutuhkan: `jobs/` (antrean + worker), `api-tokens/` (token M2M). |
| `references/` | Detail per topik: `database.md`, `auth.md`, `jobs.md`, `deployment.md`, `testing.md`, `redis-optional.md`. Baca hanya saat menyentuh topiknya. |

Bila aturan di sini berbeda dengan kode `template/`, laporkan ke user; jangan menebak.

## 0. Cara Memakai

- **Project baru**: salin `template/`, ikuti `template/README.md`. Jangan pasang addon kecuali kebutuhannya sudah ada.
- **Domain baru**: salin `app/items/` → `app/<domain>/`, sesuaikan, `alembic revision --autogenerate`, review, tambah test. Model, router, task, dan template ditemukan otomatis (`core/discovery.py`), jadi tidak ada file pusat yang diedit.
- **Review**: terapkan §1–§8, jalankan checklist §9.
- **Sebelum menyatakan selesai**: test suite lulus di MariaDB asli (`references/testing.md`).

## 1. Stack

FastAPI ≥ 0.121 · Jinja2 + htmx 2 + Alpine 3 **build CSP** · MariaDB ≥ 10.6 · SQLAlchemy 2 async + `asyncmy` (driver sync dilarang) · Alembic · Pydantic v2 · session login di tabel MariaDB. Redis tidak dipakai kecuali pemicu di `references/redis-optional.md` terukur.

## 2. Prinsip

1. **Stateless**: state antar-instance (session, rate limit, antrean, lock) di MariaDB, bukan memori proses.
2. **Tanpa premature abstraction**: abstraksi, addon, atau infrastruktur baru hanya saat kebutuhannya nyata.
3. **Integrasi pihak ketiga** lewat adapter tipis di `app/integrations/`: menerima `httpx.AsyncClient`, timeout eksplisit, tidak menyentuh DB.

## 3. Struktur & Layer

```
app/core/        config, database, discovery, templating, errors, middleware, logging, health, lifespan
app/auth/        user, session, CSRF, role, rate limit login, cleanup
app/<domain>/    router.py, service.py, models.py, schemas.py, templates/<domain>/
app/templates/   base.html, error.html, components/      app/static/  css/, js/app.js, js/alpine/<domain>.js
```

| Layer | Tanggung jawab | Dilarang |
|---|---|---|
| Router | Validasi input, panggil service (atau query langsung bila CRUD polos), render | Logika bisnis |
| Service | Logika bisnis, pemilik transaksi (§6) | Render, akses `Request` |
| Schemas | Pydantic v2, output `from_attributes=True` | Sintaks v1 |
| Models | Tabel SQLAlchemy | `commit`/`rollback` |

Dependency: `router → service → models`; `app/core` hanya mengimpor `app/core`; antar-domain satu arah tanpa siklus.
Penamaan: folder domain snake_case jamak, fragment `_nama.html`, komponen Alpine camelCase, event `HX-Trigger` kebab-case, `APIRouter(prefix="/<domain>")`.

## 4. Frontend (HTMX + Alpine + Jinja2)

- `render(request, page, fragment, ctx)` memilih fragment hanya untuk request HTMX non-history-restore dan selalu memasang `Vary: HX-Request` (→ `core/templating.py`).
- `_rows.html` meng-include `_row.html`; endpoint create merender `_row.html` yang sama.
- CSS/JS lewat `static_url('path')` (URL ber-`?v=<hash isi>`, nginx cache selamanya); waktu ditampilkan lewat filter `| localtime` (DB UTC → `DISPLAY_TIMEZONE`). → `core/templating.py`
- Paginasi keyset dengan baris "Muat lagi" (`hx-swap="outerHTML"`), maksimal 20–50 baris per response.
- Form HTMX: validasi Pydantic manual dari form-urlencoded. Error → `_form.html` status 422 + `HX-Retarget` ke form. Sukses → fragment + `HX-Trigger` kebab-case (atribut HTML jadi huruf kecil, `@itemSaved` tidak akan cocok). → `items/router.py`
- **CSP ketat**: tanpa `<script>`/`style` inline, htmx `allowEval: false`, Alpine build CSP (atribut hanya referensi properti/method). Komponen Alpine per domain di `static/js/alpine/<domain>.js`, dimuat lewat `{% block scripts %}` halaman pemakainya; `base.html` tidak diedit.
- Server → Alpine lewat header `HX-Trigger`, bukan `<script>` di fragment.

## 5. Auth (detail: `references/auth.md`)

- Session ID acak di cookie `HttpOnly; Secure; SameSite=Lax`; DB hanya menyimpan hash SHA-256. Idle timeout diperpanjang di DB, dibatasi umur mutlak; `max_age` cookie = umur mutlak.
- Rate limit login per (user, IP) + per IP, **bukan** per user saja (cegah penguncian akun orang lain). IPv6 dihitung per prefix /64; `login_failures` menyimpan `user_id`, bukan teks username.
- Dependency auth memakai **session DB sendiri** (`async_session_factory`), bukan `Depends(get_db)`, agar `db.begin()` di service tidak gagal karena autobegin.
- CSRF HMAC untuk semua non-GET (`hx-headers` di `<body>`, hidden input untuk form biasa); login memakai double-submit cookie.
- `role` disalin ke session → perubahan role/password **wajib** menghapus session user itu (`change_role`, `change_password`).
- Kebijakan password (`validate_password`) dan daftar role (`ROLES`) ditegakkan di **service**, bukan hanya di form/CLI.
- Admin pertama & reset password lewat `python -m app.auth.cli` (password dari prompt/stdin, bukan argumen).
- Otorisasi: `require_role(...)`, dan data milik user **difilter di query** (`owner_id`), bukan hanya disembunyikan di template.
- Token M2M / JWT hanya bila client-nya benar-benar ada (`addons/api-tokens`, lihat `references/auth.md`).

## 6. Database & Transaksi (detail: `references/database.md`)

- Handler memakai `DbSession` (`Depends(get_db, scope="function")`): koneksi kembali ke pool sebelum response dikirim.
- Engine web: UTC + `max_statement_time`; migrasi, skrip, dan worker memakai engine sendiri.
- Waktu antar-instance dihitung di DB (`func.now()`, `db_now_plus()`), bukan `datetime.now()`.
- **Satu pemilik transaksi** per unit of work (service, atau router bila CRUD polos). Hanya pemilik yang membuka `async with db.begin():`; fungsi lain menerima `db` aktif.
- Semua query unit of work, termasuk read, ada di dalam `begin()`. Endpoint read-only tidak perlu `begin()`.
- **Tanpa I/O jaringan atau CPU lambat di dalam transaksi**: tutup transaksi → kerja lambat → transaksi baru (contoh: `auth/service.py::login`).
- Database wajib `utf8mb4` (migrasi menolak jalan bila bukan). Input string dibatasi panjangnya sebelum INSERT: mode strict menolak "Data too long" → 500.
- Objek baru yang kolom `server_default`-nya (mis. `created_at`) dibaca setelah commit: `flush()` + `refresh()` di dalam `begin()`; tanpa itu lazy load async → `MissingGreenlet`. → `items/service.py::create_item`
- Index eksplisit, `lazy="raise"` + eager loading eksplisit, keyset pagination, hapus massal dengan `delete_in_batches()`.
- **Review setiap hasil autogenerate Alembic** (downgrade di MariaDB: `drop_table` saja, anak dulu).

## 7. Kerja di Luar Request

| Kebutuhan | Pakai |
|---|---|
| Panggil API luar dan butuh hasilnya di response | Adapter **setelah** commit, `get_http_client`, timeout ≤ 5 detik |
| Kerja ringan < 50 ms tanpa I/O luar, boleh hilang saat restart | FastAPI `BackgroundTasks` |
| I/O pihak ketiga yang harus terkirim, retry, kerja lambat, progres | Pasang `addons/jobs` (`references/jobs.md`): enqueue di transaksi yang sama dengan data bisnis |
| Pembersihan periodik | Skrip terjadwal (`python -m app.auth.cleanup`), atau `@maintenance` bila addon jobs terpasang |

## 8. Error, Security, Config

- Error handler sadar-HTMX: HTMX → toast + `HX-Retarget: #toast`; browser → `error.html`; `/api/` → JSON; detail 5xx tidak pernah bocor. `RequestValidationError` (query/path tidak valid) juga lewat jalur ini, kecuali `/api/`. Exception tak tertangani lewat `UnhandledErrorMiddleware` (paling dalam), bukan `exception_handler(Exception)`. → `core/errors.py`
- Security headers & request ID lewat pure ASGI middleware (bukan `BaseHTTPMiddleware`); request ID dibuat nginx (`$request_id`), nilai dari luar hanya dipakai bila formatnya wajar; log JSON ber-`request_id`, satu log `request` (status, `duration_ms`) per request sebagai bahan metrics. HSTS di proxy.
- Respons app default `Cache-Control: no-store` (data per user); endpoint yang aman di-cache memasang header sendiri.
- Rate limit hanya ada untuk login. Endpoint mahal (pencarian, ekspor, enqueue job) diberi `limit_req` sendiri di nginx (`references/deployment.md` §5).
- Konfigurasi lewat `pydantic-settings`. `SECRET_KEY` ≥ 32 karakter (divalidasi saat start), hanya untuk signing, bukan enkripsi. Halaman awal (redirect `/` dan setelah login) = `HOME_PATH`.
- `/readyz` memakai koneksi di luar pool web: pool penuh saat lonjakan bukan alasan mengeluarkan instance dari load balancer. Maks. satu probe DB per proses per detik, dan health check tidak dibuka ke publik (nginx `deny`).

## 9. Checklist Review

- [ ] `DbSession` di handler; dependency auth memakai session sendiri.
- [ ] Satu pemilik `begin()`; tidak ada query sebelum `begin()`; tidak ada I/O/CPU lambat di dalam transaksi.
- [ ] Index, `lazy="raise"`, keyset; migrasi autogenerate sudah di-review.
- [ ] `render()` + `Vary`; form 422 + `HX-Retarget`; `HX-Trigger` kebab-case; tanpa script/style inline.
- [ ] Data user difilter `owner_id`; `require_role` untuk aksi admin; CSRF di semua non-GET.
- [ ] Error 5xx tidak membocorkan detail; arah dependency sesuai §3.
- [ ] Addon dipakai hanya bila dibutuhkan, dan aturan di reference-nya dipenuhi.
- [ ] Test suite lulus di MariaDB asli; CI (`pip-audit`) hijau; operasional: `references/deployment.md` §8.
