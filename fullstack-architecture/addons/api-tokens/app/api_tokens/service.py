import secrets

from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api_tokens.models import ApiToken
from app.auth.service import hash_token


async def create_api_token(db: AsyncSession, client_name: str, scopes: str = "") -> str:
    """Kembalikan token mentah; tampilkan sekali ke admin, hanya hash-nya yang disimpan."""
    raw = secrets.token_urlsafe(32)
    async with db.begin():
        db.add(ApiToken(token_hash=hash_token(raw), client_name=client_name, scopes=scopes))
    return raw


async def revoke_api_token(db: AsyncSession, token_id: int) -> None:
    async with db.begin():
        await db.execute(update(ApiToken).where(ApiToken.id == token_id).values(revoked_at=func.now()))
