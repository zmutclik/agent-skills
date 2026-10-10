import hmac
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy import func, select, update

from app.auth.models import UserSession
from app.auth.service import hash_token
from app.core.config import settings
from app.core.database import async_session_factory, db_now_plus, db_time_plus

SESSION_COOKIE = "sid"
TOUCH_INTERVAL = 300   # perpanjang session / last_used maks 1x per 5 menit, bukan UPDATE tiap request


@dataclass(frozen=True)
class CurrentUser:
    id: int
    role: str
    sid: str           # hash session ID


def csrf_token_for(sid: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), sid.encode(), "sha256").hexdigest()


async def get_current_user(request: Request) -> CurrentUser:
    raw_sid = request.cookies.get(SESSION_COOKIE)
    if not raw_sid:
        raise HTTPException(status_code=401)
    sid = hash_token(raw_sid)

    # Session DB milik dependency ini sendiri, BUKAN session request (Depends(get_db)):
    # query di session request memicu autobegin sehingga db.begin() di service gagal.
    async with async_session_factory() as db, db.begin():
        row = (await db.execute(
            select(
                UserSession.user_id,
                UserSession.role,
                (UserSession.last_seen_at < db_now_plus(-TOUCH_INTERVAL)).label("stale"),
            ).where(UserSession.id == sid, UserSession.expires_at > func.now())
        )).one_or_none()
        if row is None:
            raise HTTPException(status_code=401)
        if row.stale:
            # Idle timeout diperpanjang, tapi tidak melewati batas mutlak sejak login. Cookie tidak perlu
            # diperbarui: max_age-nya sudah SESSION_MAX_AGE, server yang menentukan kapan session berakhir.
            await db.execute(
                update(UserSession).where(UserSession.id == sid)
                .values(last_seen_at=func.now(), expires_at=func.least(
                    db_now_plus(settings.SESSION_TTL),
                    db_time_plus(UserSession.created_at, settings.SESSION_MAX_AGE),
                ))
            )

    request.state.csrf_token = csrf_token_for(sid)
    return CurrentUser(id=row.user_id, role=row.role, sid=sid)


CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]


def require_role(*roles: str):
    async def checker(user: CurrentUserDep) -> CurrentUser:
        if user.role not in roles:
            raise HTTPException(status_code=403)
        return user
    return checker


async def require_csrf(request: Request, user: CurrentUserDep) -> None:
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    sent = request.headers.get("X-CSRF-Token")
    if sent is None:
        sent = (await request.form()).get("csrf_token")      # form non-HTMX
    if not isinstance(sent, str) or not hmac.compare_digest(sent, csrf_token_for(user.sid)):
        raise HTTPException(status_code=403, detail="Token CSRF tidak valid.")

