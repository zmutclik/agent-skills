# Addon `jobs`: Antrean Job di MariaDB

Pasang hanya bila ada kerja yang harus **terkirim walau proses restart**, butuh retry, lambat, atau perlu progres, misalnya I/O pihak ketiga, email, atau ekspor. Untuk kerja ringan yang boleh hilang, cukup `BackgroundTasks` (SKILL.md §7).

## 1. Memasang

```bash
cp -r <skill>/addons/jobs/. ./
uv run alembic revision --autogenerate -m "add jobs"   # review, lalu upgrade head
```

Setelah itu:
1. **`docker-compose.yml`**: tambah service worker.
   ```yaml
   worker:
     build: .
     command: ["python", "-m", "app.jobs.worker"]
     env_file: .env
     stop_grace_period: 40s          # WAJIB > WORKER_SHUTDOWN_GRACE (default 25)
     restart: unless-stopped
     logging: *default-logging       # anchor di atas docker-compose.yml
   ```
2. **`.env.example`**: `WORKER_CONCURRENCY=10`, `WORKER_SHUTDOWN_GRACE=25` (setting lain di `app/jobs/config.py`).
3. **Hapus jadwal cron** `python -m app.auth.cleanup`: `app/auth/tasks.py` menjalankannya lewat `@maintenance`.

Router, task, dan model ditemukan otomatis; `core/config.py` tidak diedit.

| File | Isi |
|---|---|
| `app/jobs/queue.py` | `enqueue()`, `@task`, `@maintenance`, `report_progress()` |
| `app/jobs/worker.py` | Loop claim, eksekusi, retry, maintenance, shutdown |
| `app/jobs/tasks.py` | `reclaim_stale`, purge job `done`/`failed` |
| `app/jobs/router.py` | Polling status `/jobs/{id}/status` |
| `app/auth/tasks.py` | Cleanup auth sebagai `@maintenance` |
| `tests/test_worker.py`, `support_tasks.py`, `worker_runner.py` | Test addon |

## 2. Memakai (outbox)

```python
# app/<domain>/service.py
from app.jobs.queue import enqueue

async with db.begin():
    db.add(order)
    await db.flush()                                   # isi order.id
    enqueue(db, "orders.push_to_erp", owner_id=user_id, order_id=order.id)
```

```python
# app/<domain>/tasks.py (ditemukan otomatis oleh worker)
from app.jobs.queue import task

@task("orders.push_to_erp")
async def push_to_erp(ctx: dict, order_id: int) -> None:
    async with ctx["db"]() as db:
        order = await db.get(Order, order_id)
    if order is None or order.pushed_at:               # idempotent: aman di-retry / di-reclaim
        return
    await erp.push(ctx["http"], order)                 # adapter app/integrations/, di luar transaksi
    async with ctx["db"]() as db, db.begin():
        await db.execute(update(Order).where(Order.id == order_id).values(pushed_at=func.now()))
```

## 3. Aturan task

- **Enqueue di transaksi yang sama** dengan data bisnis: commit gagal berarti job juga tidak ada. `enqueue()` tidak membuka transaksi sendiri.
- **Idempotent**: task bisa berjalan lebih dari sekali, jadi cek status di data bisnis sebelum efek samping.
- **Payload** hanya nilai primitif yang bisa di-JSON-kan. Enqueue dengan **nama** task, bukan mengimpor fungsinya (cegah import melingkar `service ↔ tasks`).
- **Satu koneksi DB per job pada satu waktu**: jangan `gather` beberapa session dalam satu job.
- Kerja CPU-bound via `run_in_executor(ProcessPoolExecutor)`.
- `report_progress(ctx, %)` maksimal tiap beberapa detik.
- Jadwal bisnis periodik (cron/systemd timer/CronJob) menjalankan skrip yang memanggil `enqueue()`. Worker tidak punya scheduler.
- Pembersihan periodik milik domain: `@maintenance` + `delete_in_batches(ctx["db"], stmt)`.
- `enqueue()` menyalin request ID ke `jobs.request_id`; worker memasangnya lagi, jadi log worker bisa ditelusuri ke request asal.

## 4. Desain worker (alasan di balik kode)

- **Claim**: `SELECT ... FOR UPDATE SKIP LOCKED` + **satu** `UPDATE ... WHERE id IN (...)`, sehingga worker paralel tidak saling menunggu dan tidak menjalankan job yang sama.
- **Claim per batch**: bila slot kosong < separuh `concurrency`, tunggu `CLAIM_BATCH_DELAY` (50 ms) lalu claim semua slot kosong sekaligus; tanpa ini setiap job selesai memicu satu claim berisi satu job.
- **Isolation `READ COMMITTED`** di engine worker: claim tidak mengambil gap lock yang memblok INSERT job dari web. Pool `concurrency + 2`.
- **Slot penuh**: tunggu job selesai (`FIRST_COMPLETED`) atau sinyal stop, bukan tidur. Antrean kosong: tidur `WORKER_POLL_INTERVAL`, tetap bangun bila ada sinyal stop.
- **Gagal**: retry dengan backoff eksponensial sampai `max_attempts`; task tak terdaftar langsung `failed`.
- **Maintenance** (±60 detik, task terpisah dari loop claim): hanya worker pemegang `GET_LOCK(CONCAT(DATABASE(), '.maintenance'))` yang menjalankannya (nama lock berlaku se-server, jadi diberi prefix nama DB).
- **Error DB sesaat** (koneksi putus, deadlock, failover): claim yang gagal dicatat lalu dicoba lagi dengan backoff (maks. `MAX_CLAIM_BACKOFF` 30 detik); worker dan job yang sedang berjalan tidak ikut mati. Gagal menulis status akhir job juga hanya dicatat; job itu diambil lagi oleh `reclaim_stale`.
- **Worker mati mendadak**: `reclaim_stale` mengembalikan job yang `locked_until`-nya lewat.
- **SIGTERM**: berhenti claim, tunggu job sampai `WORKER_SHUTDOWN_GRACE`, sisanya kembali ke `queued` tanpa menambah `attempts`.
- Worker terpisah per jenis task hanya bila job lambat mulai menahan job cepat.

## 5. Polling status & operasional

- `/jobs/{id}/status`: satu lookup PK + cek `owner_id`; `hx-trigger="every 3s"` atau lebih longgar; HTTP 286 saat `done`/`failed` agar HTMX berhenti polling.
- Koneksi DB: tambahkan `replicas × worker_processes × (WORKER_CONCURRENCY + 2)` ke rumus `database.md` §2.
- Grace period orchestrator (`stop_grace_period` / `terminationGracePeriodSeconds`) **>** `WORKER_SHUTDOWN_GRACE`.
- Metrics: jumlah `queued`, umur `queued` tertua (lag), jumlah `failed`, saturasi pool worker.
- Lag tinggi atau throughput ratusan job/detik: lihat `redis-optional.md`.

## 6. Test wajib (sudah ada di `tests/test_worker.py`)

Outbox ikut rollback dan request ID terbawa · polling status hanya untuk pemilik dan 286 saat selesai · 3 worker × 300 job, setiap job tepat sekali · claim per batch (≥ 5 job per claim) · retry + backoff · worker bertahan saat claim gagal sesaat · task tak terdaftar langsung `failed` · reclaim job macet · HTTP client diinjeksi (`httpx.MockTransport`, tanpa API sungguhan) · SIGTERM pada proses asli · purge backlog > `BATCH_LIMIT` dalam satu putaran · maintenance hanya pada pemegang `GET_LOCK` · cleanup auth jalan sebagai maintenance.
