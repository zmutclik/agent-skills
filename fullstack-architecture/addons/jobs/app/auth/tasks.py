from app.auth.cleanup import purge_auth_rows
from app.jobs.queue import maintenance


@maintenance
async def purge_auth_rows_periodically(ctx: dict) -> None:
    # Menggantikan jadwal cron `python -m app.auth.cleanup` selama worker berjalan
    await purge_auth_rows(ctx["db"])
