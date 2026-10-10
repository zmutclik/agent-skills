"""Worker job queue MariaDB. Jalankan: python -m app.jobs.worker"""
import asyncio
import logging
import signal

import httpx
from sqlalchemy import Row, func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.core.database import db_now_plus, make_engine
from app.core.discovery import import_domain_modules
from app.core.logging import request_id_var, setup_logging
from app.jobs.config import job_settings
from app.jobs.models import Job
from app.jobs.queue import MAINTENANCE, TASKS

logger = logging.getLogger(__name__)

MAINTENANCE_INTERVAL = 60
# GET_LOCK: satu worker per putaran. Nama lock berlaku se-server MariaDB, jadi diberi prefix nama database
MAINTENANCE_LOCK = func.concat(func.database(), ".maintenance")
CLAIM_BATCH_DELAY = 0.05               # tunggu sebentar agar satu claim mengambil beberapa slot sekaligus
MAX_CLAIM_BACKOFF = 30                 # detik; batas jeda saat claim gagal berturut-turut (DB down)


async def claim(db_factory, limit: int) -> list[Row]:
    async with db_factory() as db, db.begin():
        jobs = (await db.execute(
            select(Job.id, Job.task, Job.payload, Job.attempts, Job.max_attempts, Job.request_id)
            .where(Job.status == "queued", Job.run_after <= func.now())
            .order_by(Job.run_after)                  # mengikuti urutan index ix_jobs_claim
            .limit(limit)
            .with_for_update(skip_locked=True)        # worker paralel tidak saling menunggu
        )).all()
        if jobs:
            # Satu UPDATE untuk semua job, bukan satu per job: row lock dilepas secepatnya
            await db.execute(
                update(Job).where(Job.id.in_([job.id for job in jobs]))
                .values(status="running", attempts=Job.attempts + 1,
                        locked_until=db_now_plus(job_settings.JOB_TIMEOUT + 60))
            )
    return jobs


async def execute(ctx: dict, job: Row) -> None:
    request_id_var.set(job.request_id)
    fn = TASKS.get(job.task)
    try:
        if fn is None:
            raise LookupError(f"task tidak terdaftar: {job.task}")
        job_ctx = {**ctx, "job_id": job.id}          # untuk report_progress()
        await asyncio.wait_for(fn(job_ctx, **job.payload), timeout=job_settings.JOB_TIMEOUT)
        values = {"status": "done", "progress": 100, "locked_until": None}
    except Exception as err:
        logger.exception("job_failed", extra={"job_id": job.id, "task": job.task})
        attempt = job.attempts + 1                    # `job` dibaca sebelum UPDATE di claim
        retry = fn is not None and attempt < job.max_attempts
        values = {
            "status": "queued" if retry else "failed",
            "run_after": db_now_plus(10 * 2 ** attempt),         # backoff eksponensial
            "locked_until": None,
            "last_error": repr(err)[:2000],
        }
    try:
        async with ctx["db"]() as db, db.begin():
            await db.execute(update(Job).where(Job.id == job.id).values(**values))
    except Exception:
        # Job tetap 'running'; reclaim_stale mengembalikannya setelah locked_until lewat (task harus idempotent)
        logger.exception("job_status_update_failed", extra={"job_id": job.id, "task": job.task})


async def run_maintenance(engine: AsyncEngine) -> bool:
    """Jalankan semua fungsi @maintenance bila lock didapat. Worker lain melewati putaran ini, sehingga
    DELETE/UPDATE pembersihan tidak saling menunggu row lock. Lock lepas otomatis bila koneksi putus."""
    async with engine.connect() as conn:
        acquired = await conn.scalar(select(func.get_lock(MAINTENANCE_LOCK, 0)))
        await conn.commit()
        if not acquired:
            return False
        # Semua fungsi memakai koneksi pemegang lock: tidak butuh slot pool tambahan
        ctx = {"db": async_sessionmaker(conn, autoflush=False, expire_on_commit=False)}
        try:
            for fn in MAINTENANCE:
                try:
                    await fn(ctx)
                except Exception:
                    logger.exception("maintenance_failed", extra={"fn": fn.__name__})
        finally:
            await conn.scalar(select(func.release_lock(MAINTENANCE_LOCK)))
            await conn.commit()
    return True


async def maintenance_loop(engine: AsyncEngine, stop: asyncio.Event) -> None:
    # Task terpisah dari loop claim: pembersihan tidak menunda claim, dan tetap jalan saat semua slot penuh
    while not stop.is_set():
        try:
            await run_maintenance(engine)
        except Exception:
            logger.exception("maintenance_failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=MAINTENANCE_INTERVAL)
        except TimeoutError:
            pass


async def shutdown(ctx: dict, running: dict[asyncio.Task, int]) -> None:
    """Tunggu job berjalan sampai WORKER_SHUTDOWN_GRACE; sisanya langsung dikembalikan ke antrean
    agar tidak menunggu locked_until (JOB_TIMEOUT + 60 detik) sebelum diambil worker lain."""
    if not running:
        return
    _, pending = await asyncio.wait(running.keys(), timeout=job_settings.WORKER_SHUTDOWN_GRACE)
    interrupted = [running[t] for t in pending]
    for t in pending:
        t.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    if interrupted:
        async with ctx["db"]() as db, db.begin():
            await db.execute(
                update(Job).where(Job.id.in_(interrupted))
                .values(status="queued", attempts=Job.attempts - 1, locked_until=None)  # interupsi ≠ gagal
            )
        logger.info("jobs_requeued_on_shutdown", extra={"job_ids": interrupted})


async def run(stop: asyncio.Event, *, concurrency: int | None = None,
              http_client: httpx.AsyncClient | None = None) -> None:
    import_domain_modules("tasks")                    # mengisi TASKS & MAINTENANCE
    concurrency = concurrency or job_settings.WORKER_CONCURRENCY
    claim_batch = max(1, concurrency // 2)
    engine = make_engine(
        pool_size=concurrency + 2,                    # 1 per job paralel + loop claim + maintenance
        max_overflow=0,
        statement_timeout=job_settings.DB_JOB_STATEMENT_TIMEOUT,
        isolation_level="READ COMMITTED",             # tanpa gap lock: claim tidak memblok INSERT job dari web
    )
    http = http_client or httpx.AsyncClient(timeout=10.0)
    ctx = {"db": async_sessionmaker(engine, autoflush=False, expire_on_commit=False), "http": http}

    running: dict[asyncio.Task, int] = {}             # task → job_id
    claim_failures = 0
    stop_waiter = asyncio.create_task(stop.wait())
    maintenance_task = asyncio.create_task(maintenance_loop(engine, stop))
    try:
        while not stop.is_set():
            free = concurrency - len(running)
            if free == 0:
                # Slot penuh: lanjut begitu ada job selesai (bukan tidur) atau ada sinyal stop
                await asyncio.wait([*running.keys(), stop_waiter], return_when=asyncio.FIRST_COMPLETED)
                continue
            if free < claim_batch:
                # Kumpulkan slot kosong sebentar: satu claim per beberapa job, bukan satu claim per job selesai.
                # Setelah jeda tetap claim slot yang ada, sehingga job panjang tidak membuat slot lain menganggur.
                await asyncio.wait([stop_waiter], timeout=CLAIM_BATCH_DELAY)
                if stop.is_set():
                    break
                free = concurrency - len(running)

            try:
                jobs = await claim(ctx["db"], free)
            except Exception:
                # Error DB sesaat (koneksi putus, deadlock, failover) tidak boleh mematikan worker beserta job
                # yang sedang berjalan: catat, tunggu dengan backoff, coba lagi.
                claim_failures += 1
                logger.exception("claim_failed", extra={"consecutive_failures": claim_failures})
                delay = min(job_settings.WORKER_POLL_INTERVAL * 2 ** claim_failures, MAX_CLAIM_BACKOFF)
                await asyncio.wait([stop_waiter], timeout=delay)
                continue
            claim_failures = 0
            for job in jobs:
                t = asyncio.create_task(execute(ctx, job))
                running[t] = job.id
                t.add_done_callback(lambda done: running.pop(done, None))
            if not jobs:
                # Antrean kosong: tidur, tapi bangun bila ada sinyal stop
                await asyncio.wait([stop_waiter], timeout=job_settings.WORKER_POLL_INTERVAL)
    finally:
        stop_waiter.cancel()
        maintenance_task.cancel()                     # DELETE per batch aman dibatalkan (rollback)
        await asyncio.gather(maintenance_task, return_exceptions=True)
        await shutdown(ctx, running)
        if http_client is None:
            await http.aclose()
        await engine.dispose()


def main() -> None:
    setup_logging()

    async def _main() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        logger.info("worker_started")
        await run(stop)
        logger.info("worker_stopped")

    asyncio.run(_main())


if __name__ == "__main__":
    main()
