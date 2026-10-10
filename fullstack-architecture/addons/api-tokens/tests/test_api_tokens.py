import pytest
from fastapi import HTTPException
from sqlalchemy import select
from starlette.requests import Request

from app.api_tokens import service as token_service
from app.api_tokens.dependencies import get_api_client, require_scope
from app.api_tokens.models import ApiToken
from app.auth.service import hash_token
from app.core.database import async_session_factory


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
