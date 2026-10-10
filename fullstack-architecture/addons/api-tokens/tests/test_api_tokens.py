import asyncio
import sys

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, text, update
from starlette.requests import Request

from app.api_tokens import service as token_service
from app.api_tokens.dependencies import get_api_client, require_scope
from app.api_tokens.models import ApiToken
from app.auth.service import hash_token
from app.core.database import async_session_factory
from tests.conftest import PROJECT_DIR


def bearer(raw: str) -> Request:
    return Request({"type": "http", "headers": [(b"authorization", f"Bearer {raw}".encode())]})


async def test_token_stored_as_hash_and_scope_checked():
    async with async_session_factory() as db:
        raw = await token_service.create_api_token(db, "erp", scopes="items:read")
    async with async_session_factory() as db:
        assert (await db.scalar(select(ApiToken))).token_hash == hash_token(raw)

    client = await get_api_client(bearer(raw))
    assert client.client_name == "erp"
    assert await require_scope("items:read")(client) == client
    with pytest.raises(HTTPException) as exc:
        await require_scope("items:write")(client)
    assert exc.value.status_code == 403


async def test_revocation_is_immediate():
    async with async_session_factory() as db:
        raw = await token_service.create_api_token(db, "erp")
    await get_api_client(bearer(raw))

    async with async_session_factory() as db:
        token_id = await db.scalar(select(ApiToken.id))
    async with async_session_factory() as db:
        await token_service.revoke_api_token(db, token_id)
    with pytest.raises(HTTPException) as exc:
        await get_api_client(bearer(raw))
    assert exc.value.status_code == 401


async def test_expired_token_rejected():
    async with async_session_factory() as db:
        raw = await token_service.create_api_token(db, "erp", expires_in=3600)
    await get_api_client(bearer(raw))
    async with async_session_factory() as db, db.begin():
        await db.execute(update(ApiToken).values(expires_at=text("NOW() - INTERVAL 1 SECOND")))
    with pytest.raises(HTTPException) as exc:
        await get_api_client(bearer(raw))
    assert exc.value.status_code == 401


async def cli(*args: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "app.api_tokens.cli", *args, cwd=PROJECT_DIR,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode, out.decode(), err.decode()


async def test_cli_issues_lists_and_revokes_tokens():
    code, out, err = await cli("create", "erp", "--scopes", "items:read  items:write", "--expires-days", "30")
    assert code == 0, err
    raw = out.strip()                                     # stdout = token saja
    client = await get_api_client(bearer(raw))
    assert client.scopes == {"items:read", "items:write"}
    async with async_session_factory() as db:
        days = await db.scalar(select(func.timestampdiff(text("DAY"), func.now(), ApiToken.expires_at)))
    assert days in (29, 30)

    code, out, _ = await cli("list")
    assert code == 0 and "erp\tactive" in out and raw not in out      # token mentah tidak pernah dicetak ulang

    assert (await cli("revoke", str(client.id)))[0] == 0
    with pytest.raises(HTTPException):
        await get_api_client(bearer(raw))
    assert (await cli("revoke", str(client.id)))[0] != 0           # sudah dicabut
    assert (await cli("create", "x" * 101))[0] != 0                 # > panjang kolom: ditolak, bukan error DB
