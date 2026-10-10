import asyncio
import json
import logging
import time
from contextlib import AsyncExitStack
from datetime import datetime

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import ValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.core import health
from app.core.config import Settings, settings
from app.core.database import engine
from app.core.logging import JsonFormatter, request_id_var
from app.core.templating import localtime, static_url
from app.main import app
from tests.conftest import PROJECT_DIR, htmx_headers


async def test_health_endpoints(client):
    assert (await client.get("/healthz")).json() == {"status": "ok"}
    assert (await client.get("/readyz")).json() == {"status": "ready"}
    assert (await client.head("/healthz")).status_code == 200


async def test_readiness_probes_db_at_most_once_per_interval(client, monkeypatch):
    # /readyz yang dipanggil beruntun (atau terbuka ke publik) tidak boleh membuka koneksi DB per request
    calls = 0

    async def counting_ping():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)

    monkeypatch.setattr(health, "_ping_db", counting_ping)
    monkeypatch.setattr(health, "_last_probe", (float("-inf"), False))
    responses = await asyncio.gather(*(client.get("/readyz") for _ in range(20)))
    assert all(r.status_code == 200 for r in responses) and calls == 1


async def test_readiness_ignores_saturated_web_pool(client, monkeypatch):
    # Pool web penuh (lonjakan trafik) bukan alasan mengeluarkan instance dari load balancer
    monkeypatch.setattr(health, "_last_probe", (float("-inf"), False))   # paksa probe sungguhan, bukan cache
    async with AsyncExitStack() as stack:
        for _ in range(settings.DB_POOL_SIZE + settings.DB_MAX_OVERFLOW):
            await stack.enter_async_context(engine.connect())
        started = time.monotonic()
        resp = await client.get("/readyz")
    assert resp.status_code == 200 and time.monotonic() - started < 2


@pytest.fixture
def typed_routes():
    async def typed(n: int):
        return {"n": n}
    app.add_api_route("/__typed", typed)
    app.add_api_route("/api/__typed", typed)
    yield
    del app.router.routes[-2:]


async def test_invalid_query_param_is_toast_not_raw_json(client, typed_routes):
    resp = await client.get("/__typed?n=abc", headers=htmx_headers())
    assert resp.status_code == 422 and resp.headers["HX-Retarget"] == "#toast"
    assert "toast" in resp.text and "int_parsing" not in resp.text      # detail Pydantic tidak masuk ke halaman

    resp = await client.get("/__typed?n=abc")
    assert resp.status_code == 422 and "<html" in resp.text

    resp = await client.get("/api/__typed?n=abc")                     # API tetap dapat detail validasi
    assert resp.status_code == 422 and resp.json()["detail"][0]["loc"] == ["query", "n"]


async def test_security_and_request_id_headers(client):
    resp = await client.get("/healthz", headers={"X-Request-ID": "abc123"})
    assert "script-src 'self'" in resp.headers["content-security-policy"]
    assert "unsafe-eval" not in resp.headers["content-security-policy"]
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["x-request-id"] == "abc123"


async def test_malformed_request_id_is_replaced(client):
    for bad in ("a" * 65, "x y", "a/b", "<script>"):
        rid = (await client.get("/healthz", headers={"X-Request-ID": bad})).headers["x-request-id"]
        assert rid != bad and len(rid) == 32              # diganti uuid4().hex


async def test_db_session_settings_utc_and_statement_timeout():
    async with engine.connect() as conn:
        tz, timeout = (await conn.execute(text("SELECT @@session.time_zone, @@session.max_statement_time"))).one()
    assert tz == "+00:00" and float(timeout) == 2.5


async def test_database_is_utf8mb4_and_stores_emoji():
    # Tabel mewarisi charset database; migrations/env.py menolak database selain utf8mb4
    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT @@character_set_database"))).scalar() == "utf8mb4"
        await conn.execute(text("INSERT INTO users (username, password_hash, role) VALUES ('kursi 🪑', 'x', 'user')"))
        assert (await conn.execute(text("SELECT username FROM users"))).scalar() == "kursi 🪑"
        await conn.rollback()


async def test_migration_records_version():
    # DDL MariaDB auto-commit, baris alembic_version tidak: tanpa commit di migrations/env.py tabel terbentuk
    # tapi versinya kosong, dan `alembic upgrade head` deploy berikutnya gagal "Table already exists".
    head = ScriptDirectory.from_config(Config(f"{PROJECT_DIR}/alembic.ini")).get_current_head()
    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar() == head


async def test_statement_timeout_stops_slow_query():
    started = time.monotonic()
    async with engine.connect() as conn:
        try:
            await conn.execute(text("SELECT SLEEP(5)"))
        except Exception:
            pass                       # MariaDB error 1969 (query interrupted) juga hasil yang benar
    assert time.monotonic() - started < 4   # dihentikan di ±2.5 detik, bukan menunggu 5 detik


async def test_htmx_404_rendered_as_toast(client):
    resp = await client.get("/tidak-ada", headers=htmx_headers())
    assert resp.status_code == 404
    assert resp.headers["HX-Retarget"] == "#toast" and "toast" in resp.text


async def test_browser_404_rendered_as_page(client):
    resp = await client.get("/tidak-ada")
    assert resp.status_code == 404 and "<html" in resp.text


@pytest.fixture
def boom_route():
    async def boom():
        raise RuntimeError("detail-rahasia")
    app.add_api_route("/__boom", boom)
    yield
    app.router.routes.pop()


@pytest.fixture
def error_log():
    """Catat (pesan, request_id) saat log dibuat: request_id dibaca dari contextvar, bukan dari record."""
    seen: list[tuple[str, str | None]] = []

    class Capture(logging.Handler):
        def emit(self, record):
            seen.append((record.getMessage(), request_id_var.get()))

    handler = Capture()
    logging.getLogger("app.core.errors").addHandler(handler)
    yield seen
    logging.getLogger("app.core.errors").removeHandler(handler)


async def test_unhandled_error_keeps_security_headers_and_request_id(client, boom_route, error_log):
    resp = await client.get("/__boom", headers={"X-Request-ID": "err123"})
    assert resp.status_code == 500 and "<html" in resp.text
    assert "detail-rahasia" not in resp.text
    assert "script-src 'self'" in resp.headers["content-security-policy"]
    assert resp.headers["x-request-id"] == "err123"
    assert ("unhandled_error", "err123") in error_log      # log 500 bisa ditelusuri ke request

    resp = await client.get("/__boom", headers=htmx_headers())
    assert resp.status_code == 500 and resp.headers["HX-Retarget"] == "#toast"


def test_short_secret_key_rejected():
    for weak in ("", "terlalu-pendek"):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, DATABASE_URL="mysql+asyncmy://u:p@h/db", SECRET_KEY=weak)


@pytest.fixture
def cached_route():
    async def cached():
        return JSONResponse({}, headers={"Cache-Control": "public, max-age=60"})
    app.add_api_route("/__cached", cached)
    yield
    app.router.routes.pop()


async def test_dynamic_responses_default_to_no_store(client, cached_route):
    # Data per user tidak boleh muncul lagi dari cache (tombol back setelah logout, proxy)
    assert (await client.get("/login")).headers["cache-control"] == "no-store"
    assert (await client.get("/tidak-ada")).headers["cache-control"] == "no-store"
    assert (await client.get("/__cached")).headers["cache-control"] == "public, max-age=60"   # tidak ditimpa


def test_static_url_carries_content_hash():
    url = static_url("css/app.css")
    assert url.startswith("/static/css/app.css?v=") and len(url.split("v=")[1]) == 12
    assert static_url("tidak/ada.js").endswith("?v=missing")          # vendor belum diunduh: render tetap jalan


def test_localtime_converts_utc_to_display_timezone():
    assert localtime(datetime(2026, 1, 1, 20, 30)) == "02-01-2026 03:30"   # Asia/Jakarta = UTC+7
    assert localtime(None) == ""


@pytest.fixture
def json_log():
    """Tangkap log yang sudah diformat JsonFormatter (format yang dikirim ke stdout di production)."""
    lines: list[dict] = []

    class Capture(logging.Handler):
        def emit(self, record):
            lines.append(json.loads(JsonFormatter().format(record)))

    handler = Capture()
    logging.getLogger("app.access").addHandler(handler)
    yield lines
    logging.getLogger("app.access").removeHandler(handler)


async def test_one_json_access_log_per_request(client, json_log):
    await client.get("/tidak-ada", headers={"X-Request-ID": "req-42"})
    await client.get("/healthz")                                          # health check tidak dicatat
    assert len(json_log) == 1
    entry = json_log[0]
    assert entry["msg"] == "request" and entry["request_id"] == "req-42"
    assert entry["method"] == "GET" and entry["path"] == "/tidak-ada" and entry["status"] == 404
    assert isinstance(entry["duration_ms"], float)


def test_uvicorn_logs_use_json_handler():
    # uvicorn memasang handler teks sebelum app diimpor; setup_logging mengalihkannya ke root (JSON)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        assert logging.getLogger(name).handlers == [] and logging.getLogger(name).propagate
