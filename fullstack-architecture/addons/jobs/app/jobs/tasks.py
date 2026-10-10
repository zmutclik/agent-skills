from sqlalchemy import case, delete, func, update

from app.core.database import db_now_plus, delete_in_batches
from app.jobs.models import Job
from app.jobs.queue import maintenance

DONE_RETENTION = 7 * 24 * 3600
FAILED_RETENTION = 30 * 24 * 3600   # lebih lama dari `done`: bahan investigasi


@maintenance
async def reclaim_stale(ctx: dict) -> None:
    # Worker mati tanpa shutdown bersih → job 'running' yang lewat locked_until dikembalikan
    async with ctx["db"]() as db, db.begin():
        await db.execute(
            update(Job)
            .where(Job.status == "running", Job.locked_until < func.now())
            .values(status=case((Job.attempts >= Job.max_attempts, "failed"), else_="queued"),
                    locked_until=None)
        )


@maintenance
async def purge_finished_jobs(ctx: dict) -> None:
    for status, retention in (("done", DONE_RETENTION), ("failed", FAILED_RETENTION)):
        await delete_in_batches(
            ctx["db"], delete(Job).where(Job.status == status, Job.updated_at < db_now_plus(-retention))
        )
