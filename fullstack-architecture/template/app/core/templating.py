import functools
import hashlib
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.core.config import settings
from app.core.discovery import APP_DIR


def _common_context(request: Request) -> dict:
    # csrf_token diisi get_current_user (app/auth/dependencies.py); kosong di halaman publik
    return {"csrf_token": getattr(request.state, "csrf_token", "")}


STATIC_DIR = APP_DIR / "static"


def _file_version(path: str) -> str:
    try:
        return hashlib.sha256((STATIC_DIR / path).read_bytes()).hexdigest()[:12]
    except FileNotFoundError:
        return "missing"                     # mis. vendor belum diunduh: jangan gagalkan render


_cached_file_version = functools.cache(_file_version)   # production: file static tidak berubah selama proses hidup


def static_url(path: str) -> str:
    """URL static dengan hash isi file: setelah deploy browser langsung mengambil versi baru, sehingga nginx
    boleh memberi cache panjang untuk URL ber-?v= (deploy/nginx.conf)."""
    version = _file_version(path) if settings.DEBUG else _cached_file_version(path)
    return f"/static/{path}?v={version}"


def localtime(value: datetime | None, fmt: str = "%d-%m-%Y %H:%M") -> str:
    """DATETIME di DB = UTC tanpa zona (engine memaksa time_zone +00:00) → zona tampilan DISPLAY_TIMEZONE."""
    if value is None:
        return ""
    return value.replace(tzinfo=UTC).astimezone(_display_tz).strftime(fmt)


_display_tz = ZoneInfo(settings.DISPLAY_TIMEZONE)

templates = Jinja2Templates(
    directory=[APP_DIR / "templates", *sorted(APP_DIR.glob("*/templates"))],
    context_processors=[_common_context],
)
templates.env.auto_reload = settings.DEBUG   # production: tanpa stat() file tiap render
templates.env.globals["static_url"] = static_url
templates.env.filters["localtime"] = localtime


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def is_htmx_partial(request: Request) -> bool:
    # History restore (tombol back setelah cache miss) butuh full page
    return is_htmx(request) and request.headers.get("HX-History-Restore-Request") != "true"


def render(request: Request, page: str, fragment: str, context: dict, **kwargs):
    name = fragment if is_htmx_partial(request) else page
    response = templates.TemplateResponse(request=request, name=name, context=context, **kwargs)
    response.headers["Vary"] = "HX-Request"  # cegah browser/proxy menyajikan fragment sebagai full page
    return response
