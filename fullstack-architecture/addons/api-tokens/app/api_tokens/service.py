import secrets

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api_tokens.models import ApiToken
from app.auth.service import hash_token
from app.core.database import db_now_plus


async def create_api_token(
    db: AsyncSession, client_name: str, scopes: str = "", expires_in: int | None = None
) -> str:
    """Kembalikan token mentah; tampilkan sekali ke admin, hanya hash-nya yang disimpan.
    `expires_in` dalam detik (dihitung di DB); None = tanpa kedaluwarsa, cabut manual."""
    raw = secrets.token_urlsafe(32)
    async with db.begin():
        db.add(ApiToken(
            token_hash=hash_token(raw), client_name=client_name, scopes=" ".join(scopes.split()),
            expires_at=db_now_plus(expires_in) if expires_in else None,
        ))
    return raw


async def revoke_api_token(db: AsyncSession, token_id: int) -> bool:
    """False bila token tidak ada atau sudah dicabut."""
    async with db.begin():
        result = await db.execute(
            update(ApiToken).where(ApiToken.id == token_id, ApiToken.revoked_at.is_(None))
            .values(revoked_at=func.now())
        )
    return result.rowcount > 0


async def list_api_tokens(db: AsyncSession) -> list[ApiToken]:
    async with db.begin():
        return list(await db.scalars(select(ApiToken).order_by(ApiToken.id)))
