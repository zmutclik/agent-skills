# Deployment & Operasional

File acuan: `template/Dockerfile`, `template/docker-compose.yml`, `template/deploy/nginx.conf`, `template/.env.example`.

## 1. Satu image, beberapa peran

| Peran | Perintah | Catatan |
|---|---|---|
| Web | `uvicorn app.main:app --workers $WEB_CONCURRENCY --proxy-headers --forwarded-allow-ips=<IP proxy>` | Default CMD image. |
| Migrasi | `alembic upgrade head` | Dijalankan **sekali per deploy**, sebelum web baru. Compose: `docker compose run --rm web alembic upgrade head`. |
| User | `python -m app.auth.cli create-user <nama> --role admin` / `set-password <nama>` | Admin pertama setelah migrasi awal; reset password (mencabut session). Compose: `docker compose run --rm web python -m app.auth.cli ...`. |
| Cleanup | `python -m app.auth.cleanup` | Terjadwal tiap jam (cron/systemd timer/CronJob), mis. `docker compose run --rm web python -m app.auth.cleanup`. Tidak perlu bila addon jobs terpasang. |
| Worker (addon jobs) | `python -m app.jobs.worker` | Hanya bila addon jobs dipasang (`jobs.md`). |

- `WEB_CONCURRENCY`: mulai dari jumlah core CPU. Masukkan angkanya ke rumus koneksi DB (`database.md` §2).
- **IP client** (`request.client.host`, dipakai rate limit login) hanya benar bila dua syarat terpenuhi:
  - `--forwarded-allow-ips` berisi IP/subnet proxy, **jangan `*`**. Dengan `*`, uvicorn mengambil entri `X-Forwarded-For` **paling kiri**, yang bisa diisi client (`curl -H "X-Forwarded-For: 1.2.3.4"`): lockout per-IP tertembus dan penyerang bisa memblokir IP orang lain. Compose template memakai subnet tetap `APP_SUBNET` untuk network dan untuk `FORWARDED_ALLOW_IPS`.
  - Proxy **menimpa** header (`proxy_set_header X-Forwarded-For $remote_addr`), bukan menambah (`$proxy_add_x_forwarded_for`). Ada LB di depan nginx: aktifkan `real_ip` (§5).
- `uv.lock` wajib di-commit. Dockerfile memakai `uv sync --locked`, sehingga build gagal bila lock tidak sinkron.

## 2. Urutan deploy

```
migrasi (exit 0) → web (& worker) baru → web (& worker) lama berhenti
```

- **Jangan** menjalankan `alembic upgrade` di startup aplikasi. Dengan beberapa replica, migrasi akan jalan bersamaan dan saling bentrok.
- Compose template tidak punya service migrasi: jalankan perintah di §1 secara manual; bila lupa, aplikasi jalan dengan skema lama dan error di tabel/kolom baru. Kubernetes: Job yang hanya jalan sekali per rilis.

### Migrasi tanpa downtime (expand/contract)

Selama deploy, kode lama dan kode baru berjalan bersamaan terhadap skema yang sama. Setiap migrasi harus kompatibel dengan **kedua** versi kode.

| Perubahan | Rilis 1 (expand) | Rilis 2 (contract) |
|---|---|---|
| Tambah kolom wajib | Tambah kolom `NULL`/ber-default; kode baru mengisinya; backfill per batch | Ubah jadi `NOT NULL` |
| Rename kolom | Tambah kolom baru; kode menulis ke keduanya; backfill | Kode hanya baca kolom baru; drop kolom lama |
| Hapus kolom | Kode berhenti memakai kolom | Drop kolom |
| Index di tabel besar | `ALTER TABLE ... ADD INDEX ..., ALGORITHM=INPLACE, LOCK=NONE` | — |

Review hasil autogenerate: `database.md` §3. Uji dua arah: `references/testing.md` §2.

## 3. Health check

| Endpoint | Isi | Dipakai untuk |
|---|---|---|
| `GET/HEAD /healthz` | Tanpa DB | Liveness. DB down **tidak boleh** membuat semua instance web di-restart. |
| `GET/HEAD /readyz` | `SELECT 1` lewat koneksi baru **di luar pool web** (`NullPool`); connect + query dibatasi 2 detik, jadi menjawab 503 sebelum timeout healthcheck (3 detik) | Readiness / health check load balancer: keluarkan instance dari rotasi saat DB tidak terjangkau. Jangan ukur pool web: saat lonjakan, pool semua instance penuh bersamaan dan LB akan mengeluarkan semuanya sekaligus. Maks. satu probe per proses, hasil dipakai ulang 1 detik. |

**Jangan buka health check ke publik.** `/readyz` membuka koneksi DB baru; dipanggil beruntun dari internet, endpoint ini bisa menghabiskan `max_connections`. `deploy/nginx.conf` membalas 403 untuk `/healthz` dan `/readyz` kecuali IP yang di-`allow` (isi IP LB bila LB memakai `/readyz` lewat nginx). Healthcheck compose memanggil container langsung, tidak lewat nginx.

## 4. Graceful shutdown

| Komponen | Pengaturan |
|---|---|
| Web | uvicorn menyelesaikan request berjalan saat SIGTERM; `stop_grace_period` 30s. |
| Worker (addon jobs) | Grace period orchestrator **>** `WORKER_SHUTDOWN_GRACE` (`jobs.md` §5). |

## 5. Reverse proxy (nginx)

- Static dari `app/static/` di-serve langsung oleh nginx. `app.mount("/static")` hanya aktif bila `DEBUG=true`.
- `limit_req` hanya untuk `POST /login` (lapis pertama rate limit login, `auth.md` §5): `map $request_method` memberi key kosong untuk GET, sehingga membuka halaman login tidak memakan jatah. Batasnya per IP, jadi pengguna di balik satu NAT berbagi jatah: `30r/m burst=20`; sesuaikan dengan jumlah pengguna per IP.
- Static: URL dari `static_url()` membawa `?v=<hash isi file>` dan diberi `expires max` (`map $arg_v`); tanpa `?v=` 1 jam. Deploy baru otomatis memakai CSS/JS baru tanpa menunggu cache habis. Static di-serve dari `./app/static` di host: pastikan isinya dari commit yang sama dengan image.
- `X-Request-ID` ditimpa dengan `$request_id` nginx: ID kiriman client tidak sampai ke log app dan `jobs.request_id`. Ada LB di depan yang membuat ID sendiri: teruskan header LB itu. App tetap menolak format yang tidak wajar (lapis kedua).
- **Endpoint mahal selain login** (pencarian, ekspor, upload, aksi yang meng-enqueue job): tambahkan `limit_req_zone` + `location` sendiri, pola sama dengan `/login`. Tanpa itu satu client bisa menghabiskan pool DB atau antrean.
- `X-Forwarded-For` ditimpa dengan `$remote_addr` (§1). Bila TLS diterminasi LB/Caddy **di depan** nginx, aktifkan `set_real_ip_from <IP LB>` + `real_ip_header X-Forwarded-For` (contoh di `deploy/nginx.conf`). Tanpa itu `$remote_addr` adalah IP LB: `limit_req` dan lockout per-IP berlaku untuk semua user sekaligus.
- TLS diterminasi di proxy. Tambahkan `Strict-Transport-Security` di blok server TLS. HSTS tidak dipasang di app, karena header itu hanya bermakna di HTTPS.
- `proxy_http_version 1.1` + `Connection ""` + `keepalive` di upstream, agar koneksi ke uvicorn dipakai ulang.
- **`keepalive_timeout` upstream nginx (60 s) < `--timeout-keep-alive` uvicorn (75 s, Dockerfile).** Default uvicorn 5 s: nginx kadang memakai koneksi idle yang baru ditutup uvicorn → 502. GET dicoba ulang nginx, POST tidak, jadi user melihat error acak saat submit form. Ubah salah satu angka, ubah juga yang lain.

## 6. MariaDB production

- Compose template tidak menyertakan MariaDB; server eksternal dirujuk lewat `DATABASE_URL` (versi minimum: SKILL.md §1). Pakai database khusus aplikasi, terpisah dari database test.
- `--default-time-zone=+00:00` di server (engine juga memaksanya per koneksi, `database.md` §1).
- **Charset `utf8mb4`**: `CREATE DATABASE app CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci`, atau `character-set-server=utf8mb4` di server. Default MariaDB < 11.6 upstream, RHEL, dan RDS adalah `latin1`; tabel mewarisi charset database, lalu emoji/aksara non-Latin gagal disimpan (500). `migrations/env.py` menolak migrasi bila charset database bukan `utf8mb4`.
- `max_connections` ≥ hasil rumus koneksi (`database.md` §2) + cadangan.
- Aktifkan slow query log (`slow_query_log=1`, `long_query_time=1`).
- Backup: `mariadb-backup` (fisik, untuk DB besar) atau `mariadb-dump --single-transaction` (logis) terjadwal. **Uji restore** secara berkala; backup yang belum pernah di-restore belum terbukti.

## 7. Log & metrics

- Semua log ke stdout sebagai JSON satu baris, termasuk log uvicorn (`setup_logging` mengalihkan handler-nya). Access log teks uvicorn dimatikan (`--no-access-log`); penggantinya satu log `"msg": "request"` per request dengan `method`, `path`, `status`, `duration_ms`, `request_id` (`/healthz` & `/readyz` tidak dicatat).
- Rotasi log: anchor `x-logging` di `docker-compose.yml` (json-file, 5 × 20 MB per container). Pasang di setiap service, termasuk worker.
- **Metrics dari log** (Loki, CloudWatch, Datadog, dsb.), tanpa dependency tambahan dan aman untuk `--workers` > 1:

  | Metrics | Sumber |
  |---|---|
  | Latensi p50/p95/p99 | `duration_ms` log `request` |
  | Rasio 5xx | `status >= 500` / semua log `request` |
  | Pool DB habis | log `unhandled_error` dengan `TimeoutError` SQLAlchemy (`QueuePool limit ... reached`) |
  | Query lambat | slow query log MariaDB (§6); error 1969 = kena `max_statement_time` |
  | Antrean (addon jobs) | query `jobs`: jumlah `queued`, umur `queued` tertua, jumlah `failed` (`jobs.md` §5) |

- Alert minimal: rasio 5xx, p95 latensi, `/readyz` gagal, disk DB, lag antrean.
- Prometheus (`prometheus_client` mode multiprocess) atau error tracker (Sentry) ditambahkan saat metrics dari log tidak cukup, bukan sejak awal.

## 8. Checklist go-live

Checklist review kode ada di SKILL.md §9; di sini hanya hal operasional.

- [ ] `.env` production: `SECRET_KEY` dari `secrets.token_urlsafe(48)` (aplikasi menolak < 32 karakter), `COOKIE_SECURE=true`, `DEBUG=false`.
- [ ] `FORWARDED_ALLOW_IPS` = IP/subnet proxy (bukan `*`); proxy menimpa `X-Forwarded-For`; `real_ip` aktif bila ada LB di depan nginx (§1, §5).
- [ ] `uv.lock` dan file vendor JS di-commit (§1); CI (`.github/workflows/ci.yml`) hijau di MariaDB asli, termasuk `pip-audit`; Dependabot (`.github/dependabot.yml`) aktif dan PR-nya di-review rutin.
- [ ] Database `utf8mb4` (§6); migrasi berjalan sebagai langkah deploy terpisah (§2).
- [ ] Admin pertama dibuat lewat `python -m app.auth.cli create-user` (§1).
- [ ] Cleanup auth terjadwal, atau addon jobs terpasang (§1).
- [ ] Health check load balancer ke `/readyz`, liveness ke `/healthz`; keduanya tidak bisa diakses dari internet (§3).
- [ ] Addon jobs: worker di compose/orchestrator, grace period > `WORKER_SHUTDOWN_GRACE` (§4).
- [ ] TLS + HSTS di proxy; port web dan DB tidak dipublikasikan ke internet; keepalive nginx < uvicorn (§5).
- [ ] `max_connections` ≥ rumus koneksi `database.md` §2 untuk jumlah replica final (§6).
- [ ] Rotasi log di setiap service; metrics + alert dari log JSON: latensi, rasio 5xx, pool DB habis, slow query log; antrean bila addon jobs (§7).
- [ ] Backup terjadwal + uji restore (§6).
