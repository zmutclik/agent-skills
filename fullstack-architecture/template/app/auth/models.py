from datetime import datetime

from sqlalchemy import CHAR, BigInteger, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

USERNAME_MAX_LENGTH = 100
ROLES = ("user", "admin")   # require_role(...) dan CLI hanya mengenal nilai ini; tambah role baru di sini


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(USERNAME_MAX_LENGTH), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(30), default="user")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class UserSession(Base):
    """Session login. PK = SHA-256 dari session ID di cookie (nilai asli tidak disimpan)."""

    __tablename__ = "user_sessions"

    id: Mapped[str] = mapped_column(CHAR(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(30))   # salinan users.role: hapus session saat role berubah
    expires_at: Mapped[datetime] = mapped_column(index=True)    # idle timeout, diperpanjang saat aktif
    last_seen_at: Mapped[datetime] = mapped_column(server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())   # dasar batas mutlak SESSION_MAX_AGE


class LoginFailure(Base):
    """Rate limit login tanpa Redis: setiap percobaan dicatat di sini dulu dan baru dihapus bila login
    berhasil; kegagalan dihitung per (user, IP) dan per IP dalam jendela waktu.
    Teks username tidak disimpan: user yang salah mengetik password di kolom username tidak meninggalkan
    password-nya di tabel ini. Username yang tidak ada → user_id NULL, hanya terhitung per IP."""

    __tablename__ = "login_failures"
    __table_args__ = (
        Index("ix_login_failures_ip", "ip", "created_at"),   # kedua hitungan memfilter ip
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    ip: Mapped[str] = mapped_column(String(45))   # kunci rate limit: IPv4, atau prefix /64 untuk IPv6
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)
