import asyncio
import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import case, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.auth.models import USERNAME_MAX_LENGTH, LoginFailure, User, UserSession
from app.core.config import settings
from app.core.database import db_now_plus

_hasher = PasswordHasher()
# Diverifikasi saat username tidak ada, agar waktu respons tidak membocorkan username valid
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))
# Batasi argon2 paralel: tanpa ini lonjakan login memakai seluruh threadpool (40) × 64 MiB RAM per proses
_hash_slots = asyncio.Semaphore(settings.PASSWORD_HASH_CONCURRENCY)


class TooManyAttempts(Exception):
    pass


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


async def _run_hasher(fn, *args):
    # argon2 CPU-bound (~50–300 ms): threadpool, bukan event loop; antre bila slot penuh
    async with _hash_slots:
        return await run_in_threadpool(fn, *args)


async def hash_password(password: str) -> str:
    return await _run_hasher(_hasher.hash, password)


async def verify_password(password_hash: str, password: str) -> bool:
    try:
        return await _run_hasher(_hasher.verify, password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


async def create_user(db: AsyncSession, username: str, password: str, role: str = "user") -> User:
    password_hash = await hash_password(password)       # di luar transaksi
    async with db.begin():
        user = User(username=username, password_hash=password_hash, role=role)
        db.add(user)
    return user


async def login(db: AsyncSession, username: str, password: str, ip: str) -> str | None:
    """Kembalikan session ID mentah untuk cookie, atau None bila kredensial salah."""
    # Percobaan dicatat sebagai gagal SEBELUM dihitung (commit dulu). Percobaan paralel ke-k pasti melihat
    # ≥ k baris, sehingga paling banyak LOGIN_MAX_FAILURES yang lolos ke verifikasi. Hitung-dulu-catat-belakangan
    # bisa ditembus: semua request paralel membaca hitungan lama sebelum ada yang mencatat.
    # Batas per (username, IP), bukan per username: penyerang dari IP lain tidak bisa mengunci akun korban.
    # Batas per IP yang lebih longgar menahan tebakan banyak username dari satu IP.
    if not username or len(username) > USERNAME_MAX_LENGTH:
        # Tidak mungkin user valid. Jangan sampai ke INSERT: mode strict MariaDB menolak "Data too long" → 500
        return None
    async with db.begin():
        db.add(LoginFailure(username=username, ip=ip))
    async with db.begin():
        per_ip, per_pair = (await db.execute(
            select(func.count(), func.sum(case((LoginFailure.username == username, 1), else_=0))).where(
                LoginFailure.ip == ip,
                LoginFailure.created_at > db_now_plus(-settings.LOGIN_FAILURE_WINDOW),
            )
        )).one()
        if per_pair > settings.LOGIN_MAX_FAILURES or per_ip > settings.LOGIN_MAX_FAILURES_PER_IP:
            raise TooManyAttempts
        user = await db.scalar(select(User).where(User.username == username))

    # Verifikasi hash di luar transaksi: ~100 ms CPU tidak menahan koneksi DB
    valid = await verify_password(user.password_hash if user else _DUMMY_HASH, password)
    if user is None or not valid:
        return None                                      # baris percobaan tetap ada = tercatat gagal

    async with db.begin():
        await db.execute(delete(LoginFailure).where(LoginFailure.ip == ip, LoginFailure.username == username))
        raw_sid = secrets.token_urlsafe(32)              # selalu ID baru: cegah session fixation
        db.add(UserSession(
            id=hash_token(raw_sid), user_id=user.id, role=user.role,
            expires_at=db_now_plus(settings.SESSION_TTL),
        ))
    return raw_sid


async def logout(db: AsyncSession, sid_hash: str) -> None:
    async with db.begin():
        await db.execute(delete(UserSession).where(UserSession.id == sid_hash))


async def change_role(db: AsyncSession, user_id: int, role: str) -> None:
    async with db.begin():
        user = await db.get(User, user_id)
        user.role = role
        # Session menyimpan salinan role: cabut semua agar role baru langsung berlaku
        await db.execute(delete(UserSession).where(UserSession.user_id == user_id))


async def change_password(db: AsyncSession, user_id: int, new_password: str) -> None:
    password_hash = await hash_password(new_password)   # di luar transaksi
    async with db.begin():
        await db.execute(update(User).where(User.id == user_id).values(password_hash=password_hash))
        # Cabut semua session: session yang dicuri sebelum ganti password tidak boleh tetap berlaku.
        # Pemanggil yang ingin user tetap login membuat session baru setelahnya (lewat login()).
        await db.execute(delete(UserSession).where(UserSession.user_id == user_id))

