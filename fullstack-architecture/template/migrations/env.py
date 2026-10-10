import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings
from app.core.database import Base
from app.core.discovery import import_domain_modules

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

import_domain_modules("models")   # semua tabel domain terdaftar di metadata
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=settings.DATABASE_URL, target_metadata=target_metadata,
        literal_binds=True, dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def check_database_charset(connection: Connection) -> None:
    # Tabel tanpa charset eksplisit mewarisi charset DATABASE. Default MariaDB < 11.6 upstream/RHEL/RDS = latin1:
    # input seperti emoji lalu gagal "Incorrect string value" (500). Hentikan migrasi sebelum membuat tabel.
    charset = connection.exec_driver_sql("SELECT @@character_set_database").scalar()
    if charset != "utf8mb4":
        raise RuntimeError(
            f"Charset database = {charset}, harus utf8mb4. Jalankan: ALTER DATABASE <nama_db> "
            "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci; (sebelum ada tabel, atau konversi tabelnya juga)"
        )


def do_run_migrations(connection: Connection) -> None:
    check_database_charset(connection)
    # Query di atas memicu autobegin: tutup dulu. Bila tidak, begin_transaction() Alembic menumpang transaksi itu
    # dan tidak pernah commit → DDL (auto-commit di MariaDB) jalan, tapi baris alembic_version ikut di-rollback,
    # sehingga `alembic upgrade head` berikutnya gagal "Table already exists".
    connection.commit()
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    # Engine sendiri: TANPA max_statement_time engine web (ALTER TABLE bisa lama), tetap UTC
    engine = create_async_engine(
        settings.DATABASE_URL, poolclass=pool.NullPool,
        connect_args={"init_command": "SET time_zone = '+00:00'"},
    )
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
