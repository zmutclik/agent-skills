"""Kelola token M2M dari terminal. Token mentah hanya dicetak sekali saat dibuat; yang disimpan hanya hash-nya.
    python -m app.api_tokens.cli create erp --scopes "items:read items:write" --expires-days 365
    python -m app.api_tokens.cli list
    python -m app.api_tokens.cli revoke 3                 # langsung berlaku
Docker: docker compose run --rm web python -m app.api_tokens.cli create erp --scopes items:read"""
import argparse
import asyncio
import sys

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api_tokens import service as token_service
from app.api_tokens.models import ApiToken
from app.core.database import make_engine

CLIENT_NAME_MAX_LENGTH = ApiToken.__table__.c.client_name.type.length
SCOPES_MAX_LENGTH = ApiToken.__table__.c.scopes.type.length


async def run(args: argparse.Namespace) -> None:
    engine = make_engine(pool_size=1, max_overflow=0, statement_timeout=60)
    db_factory = async_sessionmaker(engine, autoflush=False, expire_on_commit=False)
    try:
        async with db_factory() as db:
            if args.command == "create":
                expires_in = args.expires_days * 86400 if args.expires_days else None
                raw = await token_service.create_api_token(db, args.client_name, args.scopes, expires_in)
                # stdout hanya berisi token (mudah dipipe ke secret manager); keterangan ke stderr
                print(f"Token untuk {args.client_name!r} (simpan sekarang, tidak bisa ditampilkan lagi):",
                      file=sys.stderr)
                print(raw)
            elif args.command == "revoke":
                if not await token_service.revoke_api_token(db, args.token_id):
                    sys.exit(f"Token tidak ditemukan atau sudah dicabut: id={args.token_id}")
                print(f"Token dicabut: id={args.token_id}")
            else:
                for t in await token_service.list_api_tokens(db):
                    status = "revoked" if t.revoked_at else "active"
                    print(f"{t.id}\t{t.client_name}\t{status}\tscopes={t.scopes or '-'}\t"
                          f"expires={t.expires_at or '-'}\tlast_used={t.last_used_at or '-'}")
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.api_tokens.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("client_name")
    create.add_argument("--scopes", default="", help='dipisah spasi, mis. "items:read items:write"')
    create.add_argument("--expires-days", type=int, default=None, help="kosong = tanpa kedaluwarsa")
    sub.add_parser("revoke").add_argument("token_id", type=int)
    sub.add_parser("list")
    args = parser.parse_args(argv)

    if args.command == "create":
        # Batas kolom dicek di sini: mode strict MariaDB menolak nilai terlalu panjang dengan error mentah
        if not 0 < len(args.client_name) <= CLIENT_NAME_MAX_LENGTH:
            sys.exit(f"Nama client harus 1–{CLIENT_NAME_MAX_LENGTH} karakter.")
        if len(" ".join(args.scopes.split())) > SCOPES_MAX_LENGTH:
            sys.exit(f"Scopes maksimal {SCOPES_MAX_LENGTH} karakter.")
        if args.expires_days is not None and args.expires_days <= 0:
            sys.exit("--expires-days harus > 0.")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
