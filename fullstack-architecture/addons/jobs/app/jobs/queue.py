from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import request_id_var
from app.jobs.models import Job

TaskFn = Callable[..., Awaitable[None]]
TASKS: dict[str, TaskFn] = {}
MAINTENANCE: list[Callable[[dict], Awaitable[None]]] = []


def task(name: str):
    """Daftarkan fungsi job: @task("<domain>.<fungsi>")."""
    def register(fn: TaskFn) -> TaskFn:
        TASKS[name] = fn
        return fn
    return register


def maintenance(fn: Callable[[dict], Awaitable[None]]):
    """Daftarkan fungsi pembersihan periodik milik domain. Dijalankan satu worker pada satu waktu (GET_LOCK).
    Hapus massal di dalamnya memakai delete_in_batches(ctx["db"], stmt) dari app/core/database.py."""
    MAINTENANCE.append(fn)
    return fn


def enqueue(
    db: AsyncSession, name: str, *, owner_id: int | None = None, max_attempts: int = 5, **payload: Any
) -> Job:
    """Panggil di DALAM transaksi pemilik: job ikut commit/rollback bersama data bisnis (outbox)."""
    job = Job(
        task=name, payload=payload, owner_id=owner_id, max_attempts=max_attempts,
        request_id=request_id_var.get(),
    )
    db.add(job)
    return job


async def report_progress(ctx: dict, percent: int) -> None:
    """Panggil maksimal tiap beberapa detik, bukan setiap iterasi."""
    async with ctx["db"]() as db, db.begin():
        await db.execute(update(Job).where(Job.id == ctx["job_id"]).values(progress=percent))
