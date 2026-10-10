import time
from collections.abc import AsyncGenerator
from typing import Annotated, Any

from fastapi import Depends
from sqlalchemy import Delete, func, literal_column
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings


class Base(DeclarativeBase):
    pass


def make_engine(*, pool_size: int, max_overflow: int, statement_timeout: float, **kwargs: Any) -> AsyncEngine:
    return create_async_engine(
        settings.DATABASE_URL,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=5,          # fail-fast saat pool habis, jangan antre panjang
        pool_pre_ping=True,      # deteksi koneksi putus
        pool_recycle=1800,       # daur ulang sebelum wait_timeout MariaDB
        connect_args={
            "connect_timeout": 5,
            # UTC agar NOW() sama di semua instance; max_statement_time membatasi query lambat
            "init_command": f"SET time_zone = '+00:00', max_statement_time = {statement_timeout}",
        },
        **kwargs,
    )


def db_time_plus(base, seconds: int):
    """`base` (kolom atau ekspresi waktu) + N detik, dihitung di server DB."""
    return func.timestampadd(literal_column("SECOND"), seconds, base)


def db_now_plus(seconds: int):
    """NOW() + N detik dihitung di server DB: satu sumber jam untuk semua instance."""
    return db_time_plus(func.now(), seconds)


# Engine web (OLTP). Skrip/worker yang butuh query panjang membuat engine sendiri lewat make_engine().
engine = make_engine(
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    statement_timeout=settings.DB_STATEMENT_TIMEOUT,
)

async_session_factory = async_sessionmaker(
    bind=engine,
    autoflush=False,
    expire_on_commit=False,  # WAJIB: cegah lazy-load MissingGreenlet pasca commit
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory() as session:
        yield session        # hanya yield; transaksi diatur pemiliknya


# scope="function": session (dan koneksinya) ditutup begitu handler selesai,
# SEBELUM response dikirim — client lambat tidak menahan koneksi pool.
DbSession = Annotated[AsyncSession, Depends(get_db, scope="function")]


BATCH_LIMIT = 1000        # baris per DELETE: lock ditahan singkat
BATCH_BUDGET = 10         # detik per pemanggilan; sisa backlog lanjut di putaran berikutnya


async def delete_in_batches(db_factory: async_sessionmaker, stmt: Delete) -> int:
    """DELETE per batch (satu transaksi pendek per batch) sampai habis atau BATCH_BUDGET lewat.
    Kapasitas pembersihan mengikuti backlog, bukan dipatok BATCH_LIMIT per putaran."""
    deadline = time.monotonic() + BATCH_BUDGET
    total = 0
    while True:
        async with db_factory() as db, db.begin():
            deleted = (await db.execute(stmt.with_dialect_options(mysql_limit=BATCH_LIMIT))).rowcount
        total += deleted
        if deleted < BATCH_LIMIT or time.monotonic() >= deadline:
            return total
