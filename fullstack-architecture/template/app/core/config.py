from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DEBUG: bool = False
    DATABASE_URL: str                    # mysql+asyncmy://user:pass@host:3306/db
    # Signing saja (token CSRF), bukan kunci enkripsi. Kosong/pendek ditolak saat start, bukan diam-diam dipakai.
    SECRET_KEY: str = Field(min_length=32)

    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 5
    DB_STATEMENT_TIMEOUT: float = 2.5    # engine web (OLTP)

    HOME_PATH: str = "/items"            # tujuan setelah login dan GET /; ganti bila domain items dihapus
    DISPLAY_TIMEZONE: str = "Asia/Jakarta"   # hanya untuk tampilan (filter `localtime`); DB tetap UTC

    SESSION_TTL: int = 60 * 60 * 24 * 7          # idle timeout: diperpanjang selama user aktif
    SESSION_MAX_AGE: int = 60 * 60 * 24 * 30     # batas mutlak sejak login (juga max_age cookie)
    COOKIE_SECURE: bool = True
    LOGIN_MAX_FAILURES: int = 5          # per pasangan (username, IP)
    LOGIN_MAX_FAILURES_PER_IP: int = 50  # per IP untuk semua username; longgar agar kantor di balik NAT tidak terblokir
    LOGIN_FAILURE_WINDOW: int = 15 * 60
    PASSWORD_HASH_CONCURRENCY: int = 4   # argon2 paralel per proses; tiap hash ±64 MiB RAM


settings = Settings()
