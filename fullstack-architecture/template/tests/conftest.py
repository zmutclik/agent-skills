"""Test memakai MariaDB asli (bukan SQLite): time_zone, max_statement_time, perilaku lock, dan migrasi
hanya bisa diuji di engine sebenarnya. Set TEST_DATABASE_URL sebelum menjalankan pytest."""
import os
import subprocess
import sys

from sqlalchemy.engine import make_url

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
if not TEST_DATABASE_URL:
    raise RuntimeError("Set TEST_DATABASE_URL, mis. mysql+asyncmy://app:app@127.0.0.1:3306/app_test")
# Test suite menjalankan `alembic downgrade base` dan TRUNCATE semua tabel. URL yang tidak sengaja menunjuk
# DB dev/production akan menghapus seluruh datanya, jadi hanya database bernama *_test yang diterima.
_test_db_name = make_url(TEST_DATABASE_URL).database or ""
if not _test_db_name.endswith("_test"):
    raise RuntimeError(
        f"TEST_DATABASE_URL menunjuk database '{_test_db_name}'. Test suite mengosongkan database yang dipakainya; "
        "pakai database khusus test yang namanya berakhiran _test, mis. app_test."
    )

# Harus di-set sebelum modul app diimpor (settings dibaca saat import)
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ.setdefault("SECRET_KEY", "test-secret-" + "x" * 32)   # ≥ 32 karakter (config.py)
os.environ["COOKIE_SECURE"] = "false"            # test client memakai http://

import httpx  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.auth import service as auth_service  # noqa: E402
from app.auth.dependencies import SESSION_COOKIE, csrf_token_for  # noqa: E402
from app.core.database import Base, async_session_factory, engine  # noqa: E402
from app.core.discovery import import_domain_modules  # noqa: E402
from app.main import app  # noqa: E402

PROJECT_DIR = os.path.dirname(os.path.dirname(__file__))
import_domain_modules("models")


def alembic(*args: str) -> None:
    subprocess.run([sys.executable, "-m", "alembic", *args], cwd=PROJECT_DIR, check=True)


@pytest.fixture(scope="session", autouse=True)
def migrated_schema():
    # downgrade lalu upgrade: sekaligus menguji migrasi bisa dijalankan dua arah
    alembic("downgrade", "base")
    alembic("upgrade", "head")
    yield


@pytest.fixture(autouse=True)
async def clean_tables():
    yield
    # Kode yang diuji meng-commit sendiri (service pemilik transaksi), jadi bersihkan dengan TRUNCATE
    async with engine.begin() as conn:
        await conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for table in Base.metadata.sorted_tables:
            await conn.execute(text(f"TRUNCATE TABLE {table.name}"))
        await conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))


@pytest.fixture
async def client():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def make_user(username: str = "alice", password: str = "s3cret-pass", role: str = "user"):
    async with async_session_factory() as db:
        return await auth_service.create_user(db, username, password, role)


async def login(client: httpx.AsyncClient, username: str = "alice", password: str = "s3cret-pass") -> str:
    """Login lewat form asli; kembalikan token CSRF untuk request berikutnya."""
    page = await client.get("/login")
    token = page.cookies.get("login_csrf") or client.cookies.get("login_csrf")
    resp = await client.post("/login", data={"username": username, "password": password, "csrf_token": token})
    assert resp.status_code == 303, resp.text
    return csrf_token_for(auth_service.hash_token(client.cookies[SESSION_COOKIE]))


def htmx_headers(csrf: str | None = None, **extra: str) -> dict:
    headers = {"HX-Request": "true", **extra}
    if csrf:
        headers["X-CSRF-Token"] = csrf
    return headers
