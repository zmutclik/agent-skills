"""Hapus session kedaluwarsa & catatan login lama. Jalankan terjadwal (cron/systemd timer), mis. tiap jam:
    python -m app.auth.cleanup
Addon jobs menjalankan purge_auth_rows() lewat @maintenance, sehingga jadwal ini tidak diperlukan lagi."""
import asyncio

from sqlalchemy import delete, func
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.auth.models import LoginFailure, UserSession
from app.core.config import settings
from app.core.database import db_now_plus, delete_in_batches, make_engine


async def purge_auth_rows(db_factory: async_sessionmaker) -> None:
    await delete_in_batches(db_factory, delete(UserSession).where(UserSession.expires_at < func.now()))
    await delete_in_batches(
        db_factory,
        delete(LoginFailure).where(LoginFailure.created_at < db_now_plus(-settings.LOGIN_FAILURE_WINDOW)),
    )


async def _main() -> None:
    engine = make_engine(pool_size=1, max_overflow=0, statement_timeout=60)   # bukan batas OLTP engine web
    try:
        await purge_auth_rows(async_sessionmaker(engine, expire_on_commit=False))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
