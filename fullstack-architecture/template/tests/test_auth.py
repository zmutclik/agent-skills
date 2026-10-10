import asyncio
import sys
import threading
import time

import pytest
from sqlalchemy import func, select, text, update

from app.auth import service as auth_service
from app.auth.cleanup import purge_auth_rows
from app.auth.dependencies import SESSION_COOKIE, CurrentUserDep
from app.auth.models import USERNAME_MAX_LENGTH, LoginFailure, User, UserSession
from app.core.config import settings
from app.core.database import async_session_factory, db_time_plus
from app.main import app
from tests.conftest import PROJECT_DIR, htmx_headers, login, make_user


@pytest.fixture
def whoami_route():
    """Route ber-auth khusus test: test auth inti tidak bergantung pada domain contoh (items)."""
    async def whoami(user: CurrentUserDep):
        return {"id": user.id}
    app.add_api_route("/__whoami", whoami)
    yield
    app.router.routes.pop()


async def service_login(username: str, password: str, ip: str) -> str | None:
    async with async_session_factory() as db:
        return await auth_service.login(db, username, password, ip)


async def test_session_stored_as_hash(client):
    await make_user()
    await login(client)
    raw = client.cookies[SESSION_COOKIE]
    async with async_session_factory() as db:
        session = await db.scalar(select(UserSession))
    assert session.id == auth_service.hash_token(raw) and session.id != raw


async def test_login_requires_double_submit_csrf(client):
    await make_user()
    resp = await client.post("/login", data={"username": "alice", "password": "s3cret-pass", "csrf_token": "x"})
    assert resp.status_code == 403


async def test_wrong_password_then_rate_limited(client):
    await make_user()
    for _ in range(settings.LOGIN_MAX_FAILURES):
        page = await client.get("/login")
        resp = await client.post("/login", data={
            "username": "alice", "password": "salah", "csrf_token": page.cookies["login_csrf"],
        })
        assert resp.status_code == 400

    page = await client.get("/login")
    resp = await client.post("/login", data={
        "username": "alice", "password": "s3cret-pass", "csrf_token": page.cookies["login_csrf"],
    })
    assert resp.status_code == 429                       # password benar pun ditolak selama jendela


async def test_successful_login_clears_failures(client):
    await make_user()
    page = await client.get("/login")
    await client.post("/login", data={"username": "alice", "password": "salah", "csrf_token": page.cookies["login_csrf"]})
    await login(client)
    async with async_session_factory() as db:
        assert await db.scalar(select(LoginFailure)) is None


async def test_logout_deletes_session(client):
    await make_user()
    csrf = await login(client)
    resp = await client.post("/logout", data={"csrf_token": csrf})   # form non-HTMX
    assert resp.status_code == 303
    async with async_session_factory() as db:
        assert await db.scalar(select(UserSession)) is None


async def test_session_touch_is_throttled(client, whoami_route):
    await make_user()
    await login(client)

    async def last_seen():
        async with async_session_factory() as db:
            return (await db.scalar(select(UserSession))).last_seen_at

    async with async_session_factory() as db, db.begin():
        await db.execute(update(UserSession).values(last_seen_at=text("NOW() - INTERVAL 1 HOUR")))
    stale = await last_seen()
    await client.get("/__whoami")
    touched = await last_seen()
    assert touched > stale                                # session lama diperpanjang

    await client.get("/__whoami")
    assert await last_seen() == touched                   # session baru disentuh: tidak di-UPDATE lagi


async def test_login_cookie_lives_for_absolute_max_age(client):
    await make_user()
    page = await client.get("/login")
    resp = await client.post("/login", data={
        "username": "alice", "password": "s3cret-pass", "csrf_token": page.cookies["login_csrf"],
    })
    assert resp.headers["location"] == settings.HOME_PATH
    # Cookie tidak diperbarui saat session disentuh, jadi umurnya harus batas mutlak, bukan idle timeout
    assert f"Max-Age={settings.SESSION_MAX_AGE}" in resp.headers["set-cookie"]


async def test_session_extension_capped_at_max_age(client, whoami_route):
    await make_user()
    await login(client)
    async with async_session_factory() as db, db.begin():
        # Login hampir SESSION_MAX_AGE lalu, aktivitas terakhir sudah lewat TOUCH_INTERVAL
        await db.execute(update(UserSession).values(
            created_at=text(f"NOW() - INTERVAL {settings.SESSION_MAX_AGE - 60} SECOND"),
            last_seen_at=text("NOW() - INTERVAL 1 HOUR"),
        ))
    assert (await client.get("/__whoami")).status_code == 200

    async with async_session_factory() as db:
        overshoot = await db.scalar(select(func.timestampdiff(
            text("SECOND"), db_time_plus(UserSession.created_at, settings.SESSION_MAX_AGE), UserSession.expires_at,
        )))
    assert overshoot <= 0                                 # tidak diperpanjang melewati batas mutlak


async def test_role_change_revokes_sessions(client, whoami_route):
    user = await make_user()
    await login(client)
    async with async_session_factory() as db:
        await auth_service.change_role(db, user.id, "admin")
    resp = await client.get("/__whoami", headers=htmx_headers())
    assert resp.status_code == 401


async def test_lockout_is_per_username_and_ip_pair():
    await make_user()
    for _ in range(settings.LOGIN_MAX_FAILURES):
        assert await service_login("alice", "salah", "10.0.0.1") is None
    with pytest.raises(auth_service.TooManyAttempts):
        await service_login("alice", "s3cret-pass", "10.0.0.1")
    # Penyerang di 10.0.0.1 tidak bisa mengunci pemilik akun yang login dari IP lain
    assert await service_login("alice", "s3cret-pass", "10.0.0.2") is not None


async def test_lockout_per_ip_across_usernames(monkeypatch):
    monkeypatch.setattr(settings, "LOGIN_MAX_FAILURES_PER_IP", 3)
    for i in range(3):
        assert await service_login(f"user{i}", "salah", "10.0.0.1") is None
    with pytest.raises(auth_service.TooManyAttempts):    # menebak banyak username dari satu IP tetap dibatasi
        await service_login("user9", "salah", "10.0.0.1")
    assert await service_login("user9", "salah", "10.0.0.2") is None


async def test_cleanup_purges_expired_sessions_and_old_failures():
    user = await make_user()
    async with async_session_factory() as db, db.begin():
        db.add(UserSession(id="a" * 64, user_id=user.id, role="user", expires_at=text("NOW() - INTERVAL 1 MINUTE")))
        db.add(UserSession(id="b" * 64, user_id=user.id, role="user", expires_at=text("NOW() + INTERVAL 1 HOUR")))
        db.add(LoginFailure(ip="10.0.0.1", created_at=text("NOW() - INTERVAL 1 DAY")))
        db.add(LoginFailure(ip="10.0.0.2"))
    await purge_auth_rows(async_session_factory)
    async with async_session_factory() as db:
        assert list(await db.scalars(select(UserSession.id))) == ["b" * 64]
        assert list(await db.scalars(select(LoginFailure.ip))) == ["10.0.0.2"]


async def test_password_hashing_concurrency_is_bounded(monkeypatch):
    active, peak, lock = 0, 0, threading.Lock()

    class SlowHasher:
        def verify(self, password_hash, password):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return True

    monkeypatch.setattr(auth_service, "_hasher", SlowHasher())
    await asyncio.gather(*(auth_service.verify_password("h", "p") for _ in range(20)))
    assert peak == settings.PASSWORD_HASH_CONCURRENCY      # lonjakan login antre, tidak memakai seluruh threadpool


async def test_parallel_login_attempts_cannot_bypass_limit(monkeypatch):
    await make_user()
    verified = 0

    async def counting_verify(password_hash, password):
        nonlocal verified
        verified += 1
        await asyncio.sleep(0.05)
        return False

    monkeypatch.setattr(auth_service, "verify_password", counting_verify)

    async def attempt():
        async with async_session_factory() as db:
            try:
                return await auth_service.login(db, "alice", "salah", "10.0.0.1")
            except auth_service.TooManyAttempts:
                return "429"

    results = await asyncio.gather(*(attempt() for _ in range(15)))
    assert verified <= settings.LOGIN_MAX_FAILURES        # request paralel tidak melewati batas
    assert results.count("429") >= 15 - settings.LOGIN_MAX_FAILURES


async def test_overlong_username_is_rejected_not_500(client):
    # Username tidak pernah di-INSERT (login_failures menyimpan user_id), jadi panjang berapa pun tidak memicu
    # "Data too long" (mode strict) → 500
    page = await client.get("/login")
    resp = await client.post("/login", data={
        "username": "a" * (USERNAME_MAX_LENGTH + 1), "password": "x", "csrf_token": page.cookies["login_csrf"],
    })
    assert resp.status_code == 400


async def test_password_change_revokes_sessions(client, whoami_route):
    user = await make_user()
    await login(client)
    async with async_session_factory() as db:
        await auth_service.change_password(db, user.id, "password-baru-123")
    assert (await client.get("/__whoami", headers=htmx_headers())).status_code == 401
    assert await service_login("alice", "s3cret-pass", "10.0.0.1") is None
    assert await service_login("alice", "password-baru-123", "10.0.0.1") is not None


async def cli(*args: str, stdin: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "app.auth.cli", *args, cwd=PROJECT_DIR,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate(stdin.encode())
    return proc.returncode, out.decode()


async def test_cli_bootstraps_admin_and_resets_password():
    code, out = await cli("create-user", "root", "--role", "admin", stdin="rahasia-awal\n")
    assert code == 0, out
    async with async_session_factory() as db:
        assert (await db.scalar(select(User).where(User.username == "root"))).role == "admin"

    assert (await cli("create-user", "root", stdin="rahasia-awal\n"))[0] != 0      # username dipakai
    assert (await cli("create-user", "bob", stdin="pendek\n"))[0] != 0             # < 8 karakter

    session_id = await service_login("root", "rahasia-awal", "10.0.0.1")
    assert session_id is not None
    code, out = await cli("set-password", "root", stdin="rahasia-baru\n")
    assert code == 0, out
    async with async_session_factory() as db:
        assert await db.scalar(select(UserSession)) is None                       # session lama dicabut
    assert await service_login("root", "rahasia-baru", "10.0.0.1") is not None


async def test_lockout_counts_collation_variants_as_one_user():
    # Collation _ci menganggap Alice = alice: tebakan dengan variasi huruf tetap satu hitungan per user
    await make_user()
    for name in ("alice", "Alice", "ALICE", "aLice", "alicE"):
        assert await service_login(name, "salah", "10.0.0.1") is None
    with pytest.raises(auth_service.TooManyAttempts):
        await service_login("ALIce", "s3cret-pass", "10.0.0.1")


async def test_login_failures_do_not_store_typed_username():
    # User yang salah mengetik password di kolom username tidak meninggalkan password itu di DB
    assert await service_login("password-rahasia-ku", "x", "10.0.0.1") is None
    async with async_session_factory() as db:
        row = (await db.execute(text("SELECT * FROM login_failures"))).mappings().one()
    assert "password-rahasia-ku" not in map(str, row.values())


def test_rate_limit_key_groups_ipv6_by_64_prefix():
    key = auth_service.rate_limit_key
    assert key("203.0.113.9") == "203.0.113.9"
    assert key("2001:db8:1:2:aaaa::1") == key("2001:db8:1:2:ffff::9") == "2001:db8:1:2::/64"
    assert key("2001:db8:1:3::1") != key("2001:db8:1:2::1")
    assert key("::ffff:203.0.113.9") == "203.0.113.9"      # IPv4-mapped = klien IPv4
    assert key("unknown") == "unknown"


async def test_lockout_cannot_be_bypassed_by_rotating_ipv6_in_one_prefix():
    await make_user()
    for i in range(settings.LOGIN_MAX_FAILURES):
        assert await service_login("alice", "salah", f"2001:db8:1:2::{i + 1:x}") is None
    with pytest.raises(auth_service.TooManyAttempts):
        await service_login("alice", "s3cret-pass", "2001:db8:1:2:dead:beef::1")
    assert await service_login("alice", "s3cret-pass", "2001:db8:9:9::1") is not None   # prefix lain


async def test_password_policy_enforced_in_service():
    with pytest.raises(auth_service.InvalidPassword):
        await make_user(password="pendek")
    user = await make_user()
    async with async_session_factory() as db:
        with pytest.raises(auth_service.InvalidPassword):
            await auth_service.change_password(db, user.id, "pendek")
    assert await service_login("alice", "s3cret-pass", "10.0.0.1") is not None          # password lama utuh


async def test_role_must_be_known_and_user_must_exist():
    with pytest.raises(ValueError):
        await make_user(role="admn")
    user = await make_user()
    async with async_session_factory() as db:
        with pytest.raises(ValueError):
            await auth_service.change_role(db, user.id, "superuser")
        assert await auth_service.change_role(db, user.id, "user") is True              # role sama: tetap True
        assert await auth_service.change_role(db, user.id + 999, "admin") is False      # bukan AttributeError


async def test_cli_rejects_unknown_role():
    code, out = await cli("create-user", "carol", "--role", "admn", stdin="rahasia-awal\n")
    assert code != 0 and "invalid choice" in out
    async with async_session_factory() as db:
        assert await db.scalar(select(User)) is None
