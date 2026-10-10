from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy import func, or_, select, update

from app.api_tokens.models import ApiToken
from app.auth.dependencies import TOUCH_INTERVAL
from app.auth.service import hash_token
from app.core.database import async_session_factory, db_now_plus


@dataclass(frozen=True)
class ApiClient:
    id: int
    client_name: str
    scopes: frozenset[str]


async def get_api_client(request: Request) -> ApiClient:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401)
    token_hash = hash_token(auth.removeprefix("Bearer "))

    # Session DB sendiri, bukan Depends(get_db): sama alasannya dengan get_current_user
    async with async_session_factory() as db, db.begin():
        row = (await db.execute(
            select(
                ApiToken.id, ApiToken.client_name, ApiToken.scopes,
                or_(ApiToken.last_used_at.is_(None),
                    ApiToken.last_used_at < db_now_plus(-TOUCH_INTERVAL)).label("stale"),
            ).where(
                ApiToken.token_hash == token_hash,
                ApiToken.revoked_at.is_(None),
                or_(ApiToken.expires_at.is_(None), ApiToken.expires_at > func.now()),
            )
        )).one_or_none()
        if row is None:
            raise HTTPException(status_code=401)
        if row.stale:
            await db.execute(update(ApiToken).where(ApiToken.id == row.id).values(last_used_at=func.now()))

    return ApiClient(id=row.id, client_name=row.client_name, scopes=frozenset(row.scopes.split()))


def require_scope(scope: str):
    """Pakai di router /api/...: error handler mengembalikan JSON untuk path /api/."""
    async def checker(client: Annotated[ApiClient, Depends(get_api_client)]) -> ApiClient:
        if scope not in client.scopes:
            raise HTTPException(status_code=403)
        return client
    return checker
