from datetime import datetime

from sqlalchemy import CHAR, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ApiToken(Base):
    """Token M2M. Hanya hash yang disimpan; pencabutan langsung berlaku."""

    __tablename__ = "api_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(CHAR(64), unique=True)
    client_name: Mapped[str] = mapped_column(String(100))
    scopes: Mapped[str] = mapped_column(String(255), default="")   # dipisah spasi
    expires_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]
    last_used_at: Mapped[datetime | None]
