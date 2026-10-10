# Database: Detail

Aturan inti ada di SKILL.md §6. File acuan: `template/app/core/database.py`, `template/migrations/env.py`.

## 1. Engine & pool

- **`init_command`**: `SET time_zone = '+00:00', max_statement_time = N`. UTC membuat `NOW()` sama di semua instance; `max_statement_time` membatasi query lambat di engine web (`DB_STATEMENT_TIMEOUT`, default 2,5 detik).
- **Pool**: `pool_pre_ping`, `pool_recycle=1800`, `pool_timeout=5` (fail-fast), `expire_on_commit=False`. `pool_pre_ping` menambah satu round-trip per checkout sebagai ganti request gagal setelah DB restart/failover; pertahankan kecuali profiling menunjukkan ping dominan.
- **Charset**: koneksi asyncmy sudah `utf8mb4`, tapi tabel mewarisi charset **database**. Database wajib `utf8mb4` (`deployment.md` §6); `migrations/env.py` memeriksanya sebelum migrasi.
- **Engine terpisah** (`make_engine()`) untuk Alembic, skrip (`app/auth/cleanup.py`), dan worker addon jobs, karena `max_statement_time` web juga berlaku untuk `ALTER TABLE` dan kerja panjang.
- **`DbSession` dengan `scope="function"`**: koneksi dikembalikan begitu handler selesai, sebelum response dikirim; client lambat tidak menahan koneksi pool.

## 2. Kapasitas koneksi

```
replicas × WEB_CONCURRENCY × (DB_POOL_SIZE + DB_MAX_OVERFLOW)
+ probe /readyz (koneksi sesaat, 1 per health check yang sedang berjalan)
+ worker addon jobs (references/jobs.md §5)
+ cadangan (migrasi, skrip cleanup, admin, monitoring)   <  max_connections MariaDB
```

## 3. Alembic

- Template async; engine sendiri tanpa `max_statement_time`; model ditemukan lewat `import_domain_modules("models")`, termasuk model addon.
- **Review setiap hasil autogenerate.** Di MariaDB, `downgrade()` hasil autogenerate memanggil `drop_index` sebelum `drop_table` dan gagal dengan error 1553 bila index itu dipakai foreign key. Cukup `drop_table`, dari tabel anak ke tabel induk.
- Menjalankan migrasi per deploy dan pola expand/contract: `deployment.md` §2.

## 4. Query

- Index eksplisit untuk kolom filter, join, dan sort.
- Eager loading eksplisit + `lazy="raise"` pada relationship.
- Kolom `server_default` (`created_at`, dsb.) expired setelah INSERT. Bila objek baru dirender/dibaca setelah commit, `await db.flush(); await db.refresh(obj)` di dalam `begin()`. `expire_on_commit=False` tidak menolong: atribut itu memang belum pernah dimuat.
- Keyset pagination untuk tabel besar; hindari `COUNT(*)` total per request.
- **Hapus massal** dengan `delete_in_batches(db_factory, stmt)`: satu transaksi pendek per `BATCH_LIMIT` (1000) baris, diulang sampai habis atau `BATCH_BUDGET` (10 detik) lewat. Satu batch per putaran tidak cukup, karena backlog tumbuh begitu laju insert melebihi kapasitas hapus.

## 5. Transaksi: contoh pola benar

```python
# auth/service.py::login: kerja CPU lambat di antara dua transaksi pendek
async with db.begin():
    db.add(LoginFailure(username=username, ip=ip))   # catat dulu
async with db.begin():
    attempts = await db.scalar(...)                   # baru hitung
    user = await db.scalar(...)
valid = await verify_password(...)                    # argon2 di luar transaksi
async with db.begin():
    ...                                               # buat session
```

Fungsi yang membuat session sendiri dari factory (dependency auth, skrip, task, `BackgroundTasks`) adalah pemilik transaksi session tersebut.
