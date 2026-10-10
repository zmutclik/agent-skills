"""Kelola user dari terminal (admin pertama, reset password). Password dibaca dari prompt, atau satu baris stdin
bila bukan terminal (otomasi); tidak pernah dari argumen, agar tidak tercatat di riwayat shell / daftar proses.
    python -m app.auth.cli create-user admin --role admin
    python -m app.auth.cli set-password admin          # mencabut semua session user tersebut
Docker: docker compose run --rm web python -m app.auth.cli create-user admin --role admin"""
import argparse
import asyncio
import getpass
import sys

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.auth import service as auth_service
from app.auth.models import ROLES, USERNAME_MAX_LENGTH, User
from app.core.database import make_engine


def read_password() -> str:
    if not sys.stdin.isatty():
        return sys.stdin.readline().rstrip("\n")
    password = getpass.getpass("Password: ")
    if getpass.getpass("Ulangi password: ") != password:
        sys.exit("Password tidak sama.")
    return password


async def run(args: argparse.Namespace, password: str) -> None:
    engine = make_engine(pool_size=1, max_overflow=0, statement_timeout=60)
    db_factory = async_sessionmaker(engine, autoflush=False, expire_on_commit=False)
    try:
        async with db_factory() as db:
            if args.command == "create-user":
                try:
                    user = await auth_service.create_user(db, args.username, password, args.role)
                except IntegrityError:
                    sys.exit(f"Username sudah dipakai: {args.username}")
                print(f"User dibuat: id={user.id} username={user.username} role={user.role}")
            else:
                async with db.begin():                      # selesai sebelum change_password() membuka begin()
                    user_id = await db.scalar(select(User.id).where(User.username == args.username))
                if user_id is None:
                    sys.exit(f"User tidak ditemukan: {args.username}")
                await auth_service.change_password(db, user_id, password)
                print(f"Password diganti dan semua session dicabut: {args.username}")
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.auth.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create-user")
    create.add_argument("username")
    create.add_argument("--role", default="user", choices=ROLES)   # salah ketik ditolak, bukan user tanpa hak
    sub.add_parser("set-password").add_argument("username")
    args = parser.parse_args(argv)

    if not 0 < len(args.username) <= USERNAME_MAX_LENGTH:
        sys.exit(f"Username harus 1–{USERNAME_MAX_LENGTH} karakter.")
    password = read_password()
    try:
        auth_service.validate_password(password)        # cek sebelum konek DB; service tetap memeriksa ulang
    except auth_service.InvalidPassword as err:
        sys.exit(str(err))
    asyncio.run(run(args, password))


if __name__ == "__main__":
    main()
