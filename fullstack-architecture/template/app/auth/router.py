import hmac
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.auth import service as auth_service
from app.auth.dependencies import SESSION_COOKIE, CurrentUserDep, require_csrf
from app.core.config import settings
from app.core.database import DbSession
from app.core.dependencies import client_ip
from app.core.templating import templates

router = APIRouter(tags=["auth"])

LOGIN_CSRF_COOKIE = "login_csrf"


def _login_page(request: Request, error: str | None = None, status_code: int = 200):
    # Belum ada session → CSRF login memakai double-submit cookie (tanpa tulis DB)
    token = secrets.token_urlsafe(32)
    response = templates.TemplateResponse(
        request=request, name="auth/login.html",
        context={"login_csrf": token, "error": error}, status_code=status_code,
    )
    response.set_cookie(
        LOGIN_CSRF_COOKIE, token, max_age=3600, path="/login",
        httponly=True, secure=settings.COOKIE_SECURE, samesite="strict",
    )
    return response


@router.get("/login")
async def login_page(request: Request):
    return _login_page(request)


@router.post("/login")
async def login_submit(request: Request, db: DbSession):
    form = await request.form()
    cookie_token = request.cookies.get(LOGIN_CSRF_COOKIE, "")
    form_token = form.get("csrf_token")
    if not cookie_token or not isinstance(form_token, str) or not hmac.compare_digest(cookie_token, form_token):
        raise HTTPException(status_code=403, detail="Token CSRF tidak valid.")

    try:
        raw_sid = await auth_service.login(
            db, str(form.get("username", "")), str(form.get("password", "")), client_ip(request)
        )
    except auth_service.TooManyAttempts:
        return _login_page(request, "Terlalu banyak percobaan. Coba lagi nanti.", status_code=429)
    if raw_sid is None:
        return _login_page(request, "Username atau password salah.", status_code=400)

    response = RedirectResponse(settings.HOME_PATH, status_code=303)
    response.set_cookie(
        SESSION_COOKIE, raw_sid, max_age=settings.SESSION_MAX_AGE,   # batas mutlak; idle timeout dijaga server
        httponly=True, secure=settings.COOKIE_SECURE, samesite="lax",
    )
    response.delete_cookie(LOGIN_CSRF_COOKIE, path="/login")
    return response


@router.post("/logout", dependencies=[Depends(require_csrf)])
async def logout(user: CurrentUserDep, db: DbSession):
    await auth_service.logout(db, user.sid)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response
