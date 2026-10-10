"""Error handler sadar-HTMX: request HTMX dapat fragment toast, browser biasa dapat halaman, /api/ dapat JSON."""
import logging

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.templating import is_htmx, templates

logger = logging.getLogger(__name__)

MESSAGES = {
    403: "Anda tidak punya akses.",
    404: "Halaman tidak ditemukan.",
    422: "Parameter permintaan tidak valid.",
    429: "Terlalu banyak percobaan. Coba lagi nanti.",
    500: "Terjadi kesalahan di server.",
}


def error_response(request: Request, status_code: int, detail: str | None = None) -> Response:
    message = MESSAGES.get(status_code) if detail in (None, "") or status_code >= 500 else detail
    message = message or "Permintaan tidak dapat diproses."
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": message}, status_code=status_code)
    if is_htmx(request):
        # Dirender ke #toast; static/js/app.js mengizinkan swap bila HX-Retarget ada
        return templates.TemplateResponse(
            request=request, name="components/_toast.html", context={"message": message},
            status_code=status_code, headers={"HX-Retarget": "#toast", "HX-Reswap": "innerHTML"},
        )
    return templates.TemplateResponse(
        request=request, name="error.html",
        context={"message": message, "status_code": status_code}, status_code=status_code,
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        if exc.status_code == 401 and not request.url.path.startswith("/api/"):
            if is_htmx(request):
                return Response(status_code=401, headers={"HX-Redirect": "/login"})
            return RedirectResponse("/login", status_code=303)
        return error_response(request, exc.status_code, str(exc.detail) if exc.detail else None)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> Response:
        # Query/path tidak valid (mis. ?before=abc). Default FastAPI membalas JSON, dan app.js men-swap setiap 422
        # → JSON mentah masuk ke halaman. /api/ tetap dapat detail JSON; HTML/HTMX dapat halaman/toast.
        # Form HTMX tidak lewat sini: router memvalidasinya manual (items/router.py).
        if request.url.path.startswith("/api/"):
            return await request_validation_exception_handler(request, exc)
        return error_response(request, 422)


class UnhandledErrorMiddleware:
    """Pure ASGI, dipasang PALING DALAM: exception tak tertangani diubah jadi 500 di dalam middleware
    security headers & request ID. Jangan pakai `exception_handler(Exception)`: Starlette menjalankannya di
    ServerErrorMiddleware (paling luar), sehingga respons 500 tanpa CSP dan log-nya tanpa request_id."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        started = False

        async def send_tracking(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, send_tracking)
        except Exception:
            request = Request(scope)
            logger.exception("unhandled_error", extra={"path": request.url.path})
            if started:
                raise       # header sudah terkirim: tidak bisa diganti 500, biarkan server menutup koneksi
            await error_response(request, 500)(scope, receive, send)
