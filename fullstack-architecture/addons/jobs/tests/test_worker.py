import asyncio
import os
import signal
import subprocess
import sys
import time

import httpx
import pytest
from sqlalchemy import func, select, text, update

from app.auth.models import User, UserSession
from app.core import database
from app.core.database import async_session_factory, engine
from app.core.discovery import import_domain_modules
from app.core.logging import request_id_var
from app.jobs import worker
from app.jobs.config import job_settings
from app.jobs.models import Job
from app.jobs.queue import enqueue
from app.jobs.tasks import reclaim_stale
from tests import support_tasks
from tests.conftest import PROJECT_DIR, htmx_headers, login, make_user

import_domain_modules("tasks")    # MAINTENANCE terisi untuk test maintenance yang dijalankan sendiri
FAST_WORKER_ENV = {"WORKER_POLL_INTERVAL": "0.05", "WORKER_SHUTDOWN_GRACE": "1"}


@pytest.fixture(autouse=True)
def fast_worker(monkeypatch):
    for name, value in FAST_WORKER_ENV.items():
        monkeypatch.setattr(job_settings, name, float(value))


async def enqueue_many(name: str, payloads: list[dict], **opts) -> None:
    async with async_session_factory() as db, db.begin():
        for payload in payloads:
            enqueue(db, name, **opts, **payload)


async def job_statuses() -> dict[str, int]:
    async with async_session_factory() as db:
        rows = await db.execute(select(Job.status, func.count()).group_by(Job.status))
    return dict(rows.all())


async def wait_until(predicate, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while not await predicate():
        assert time.monotonic() < deadline, "timeout menunggu kondisi"
        await asyncio.sleep(0.05)


async def test_enqueue_follows_owner_transaction():
    async with async_session_factory() as db:
        with pytest.raises(RuntimeError):
            async with db.begin():
                db.add(User(username="x", password_hash="h"))
                enqueue(db, "test.record", n=1)
                await db.flush()
                raise RuntimeError("gagal setelah flush")
    assert await job_statuses() == {}                     # job ikut rollback bersama data bisnis
    async with async_session_factory() as db:
        assert await db.scalar(select(User)) is None

    token = request_id_var.set("req-123")
    try:
        await enqueue_many("test.record", [{"n": 1}])
    finally:
        request_id_var.reset(token)
    async with async_session_factory() as db:
        assert (await db.scalar(select(Job))).request_id == "req-123"   # log worker bisa ditelusuri


async def test_status_polling_owner_only_and_stops_when_done(client):
    owner = await make_user("alice")
    await make_user("bob")
    await enqueue_many("test.record", [{"n": 1}], owner_id=owner.id)
    async with async_session_factory() as db:
        job_id = await db.scalar(select(Job.id))

    await login(client, "alice")
    resp = await client.get(f"/jobs/{job_id}/status", headers=htmx_headers())
    assert resp.status_code == 200 and 'hx-trigger="every 3s"' in resp.text
    async with async_session_factory() as db, db.begin():
        await db.execute(update(Job).values(status="done"))
    assert (await client.get(f"/jobs/{job_id}/status", headers=htmx_headers())).status_code == 286

    client.cookies.clear()
    await login(client, "bob")
    assert (await client.get(f"/jobs/{job_id}/status", headers=htmx_headers())).status_code == 404


async def test_parallel_workers_run_each_job_exactly_once():
    support_tasks.executions.clear()
    await enqueue_many("test.record", [{"n": i} for i in range(300)])

    stop = asyncio.Event()
    workers = [asyncio.create_task(worker.run(stop, concurrency=5)) for _ in range(3)]
    await wait_until(lambda: _all_done(300))
    stop.set()
    await asyncio.gather(*workers)

    assert len(support_tasks.executions) == 300
    assert max(support_tasks.executions.values()) == 1      # SKIP LOCKED: tidak ada job dobel


async def _all_done(n: int) -> bool:
    return (await job_statuses()).get("done") == n


async def test_failed_job_retries_with_backoff_then_fails():
    await enqueue_many("test.fail", [{}], max_attempts=2)
    stop = asyncio.Event()
    run = asyncio.create_task(worker.run(stop, concurrency=1))

    async def attempted_once():
        async with async_session_factory() as db:
            job = await db.scalar(select(Job))
        return job.attempts == 1 and job.status == "queued"

    await wait_until(attempted_once)
    async with async_session_factory() as db:
        job = await db.scalar(select(Job))
        delay = await db.scalar(select(func.timestampdiff(text("SECOND"), func.now(), Job.run_after)))
    assert "selalu gagal" in job.last_error and delay > 10        # backoff, bukan langsung diulang

    # Majukan run_after agar percobaan kedua (terakhir) langsung jalan
    async with async_session_factory() as db, db.begin():
        await db.execute(update(Job).values(run_after=func.now()))
    await wait_until(lambda: _status_is("failed"))
    stop.set()
    await run


async def _status_is(status: str) -> bool:
    return list(await job_statuses()) == [status]


async def test_unknown_task_fails_without_retry():
    await enqueue_many("tidak.ada", [{}])
    stop = asyncio.Event()
    run = asyncio.create_task(worker.run(stop, concurrency=1))
    await wait_until(lambda: _status_is("failed"))
    stop.set()
    await run


async def test_reclaim_stale_running_job():
    await enqueue_many("test.record", [{"n": 1}])
    async with async_session_factory() as db, db.begin():
        # Simulasi worker mati: job 'running' dengan lock yang sudah kedaluwarsa
        await db.execute(update(Job).values(
            status="running", attempts=1, locked_until=text("NOW() - INTERVAL 1 MINUTE")))
    await reclaim_stale({"db": async_session_factory})
    assert await job_statuses() == {"queued": 1}


async def test_http_client_is_injected_into_tasks():
    await enqueue_many("test.call_api", [{"path": "/ping"}])
    calls = []
    transport = httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(200))
    async with httpx.AsyncClient(transport=transport) as http:
        stop = asyncio.Event()
        run = asyncio.create_task(worker.run(stop, concurrency=1, http_client=http))
        await wait_until(lambda: _status_is("done"))
        stop.set()
        await run
    assert [c.url.path for c in calls] == ["/ping"]          # API sungguhan tidak pernah dipanggil


async def test_sigterm_requeues_running_jobs_quickly():
    await enqueue_many("test.sleep", [{"seconds": 30}] * 3)
    env = {**os.environ, **FAST_WORKER_ENV, "PYTHONPATH": PROJECT_DIR}
    proc = subprocess.Popen([sys.executable, "-m", "tests.worker_runner"], cwd=PROJECT_DIR, env=env)
    try:
        async def all_reported():                          # report_progress dipanggil di awal task
            async with async_session_factory() as db:
                return set(await db.scalars(select(Job.progress))) == {10}

        await wait_until(all_reported)
        assert await job_statuses() == {"running": 3}

        started = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        await asyncio.to_thread(proc.wait, 10)
        elapsed = time.monotonic() - started
    finally:
        proc.kill()

    assert proc.returncode == 0
    assert elapsed < 5                                    # WORKER_SHUTDOWN_GRACE=1 di test
    async with async_session_factory() as db:
        jobs = list(await db.scalars(select(Job)))
    assert {j.status for j in jobs} == {"queued"}
    assert {j.attempts for j in jobs} == {0}              # interupsi tidak dihitung sebagai gagal


async def test_worker_survives_transient_claim_error(monkeypatch):
    original_claim = worker.claim
    failures = 0

    async def flaky_claim(db_factory, limit):
        nonlocal failures
        if failures < 2:                                  # mis. koneksi putus / failover DB
            failures += 1
            raise ConnectionError("db hilang sesaat")
        return await original_claim(db_factory, limit)

    monkeypatch.setattr(worker, "claim", flaky_claim)
    await enqueue_many("test.record", [{"n": 1}])
    stop = asyncio.Event()
    run = asyncio.create_task(worker.run(stop, concurrency=1))
    await wait_until(lambda: _status_is("done"))
    assert not run.done()                                 # worker tetap hidup setelah error
    stop.set()
    await run


async def test_claims_are_batched_not_one_per_finished_job(monkeypatch):
    support_tasks.executions.clear()
    claims = []
    original_claim = worker.claim

    async def counting_claim(db_factory, limit):
        jobs = await original_claim(db_factory, limit)
        if jobs:
            claims.append(len(jobs))
        return jobs

    monkeypatch.setattr(worker, "claim", counting_claim)
    await enqueue_many("test.record", [{"n": i} for i in range(200)])
    stop = asyncio.Event()
    run = asyncio.create_task(worker.run(stop, concurrency=10))
    await wait_until(lambda: _all_done(200))
    stop.set()
    await run

    assert sum(claims) == 200
    assert len(claims) <= 200 / 5            # rata-rata ≥ 5 job per claim; tanpa batching banyak claim berisi 1 job


async def _insert_old_rows(n: int) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(
            "INSERT INTO jobs (task, payload, status, attempts, max_attempts, progress, updated_at)"
            " SELECT 'x', '{}', IF(seq % 2, 'done', 'failed'), 1, 1, 100, NOW() - INTERVAL 40 DAY"
            f" FROM seq_1_to_{n}"
        ))


async def test_purge_drains_backlog_beyond_one_batch(monkeypatch):
    monkeypatch.setattr(database, "BATCH_LIMIT", 10)
    await _insert_old_rows(95)                              # done + failed, lewat retensi
    await enqueue_many("test.record", [{"n": 1}])            # job baru: tidak boleh terhapus
    assert await worker.run_maintenance(engine) is True
    assert await job_statuses() == {"queued": 1}             # 95 baris habis dalam satu putaran (10 batch)


async def test_maintenance_runs_on_one_worker_at_a_time():
    await _insert_old_rows(4)
    async with engine.connect() as holder:                   # worker lain sedang memegang lock
        assert await holder.scalar(select(func.get_lock(worker.MAINTENANCE_LOCK, 0))) == 1
        assert await worker.run_maintenance(engine) is False
        assert sum((await job_statuses()).values()) == 4     # putaran ini dilewati
        await holder.scalar(select(func.release_lock(worker.MAINTENANCE_LOCK)))
    assert await worker.run_maintenance(engine) is True
    assert await job_statuses() == {}


async def test_auth_cleanup_runs_as_maintenance():
    user = await make_user()
    async with async_session_factory() as db, db.begin():
        db.add(UserSession(id="a" * 64, user_id=user.id, role="user", expires_at=text("NOW() - INTERVAL 1 MINUTE")))
        db.add(UserSession(id="b" * 64, user_id=user.id, role="user", expires_at=text("NOW() + INTERVAL 1 HOUR")))
    await worker.run_maintenance(engine)
    async with async_session_factory() as db:
        assert list(await db.scalars(select(UserSession.id))) == ["b" * 64]
