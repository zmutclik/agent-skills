import asyncio
import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings

router = APIRouter(tags=["health"])

# Koneksi baru per pemeriksaan, di luar pool web. Readiness menjawab "DB terjangkau?", bukan "pool sedang penuh?":
# saat lonjakan trafik pool semua instance penuh bersamaan, dan bila readiness ikut gagal, load balancer
# mengeluarkan SEMUA instance sekaligus (lonjakan berubah jadi outage total).
_probe_engine = create_async_engine(
    settings.DATABASE_URL, poolclass=pool.NullPool, connect_args={"connect_timeout": 2},
)


# GET + HEAD: sebagian load balancer melakukan health check dengan HEAD
@router.api_route("/healthz", methods=["GET", "HEAD"])
async def liveness() -> dict:
    # Tanpa DB: DB down tidak boleh membuat orchestrator me-restart semua instance web
    return {"status": "ok"}


async def _ping_db() -> None:
    async with _probe_engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


# Maks. satu probe per proses, hasilnya dipakai ulang PROBE_CACHE detik: panggilan beruntun (atau /readyz yang
# terlanjur terbuka ke publik) tidak bisa membuka koneksi DB tanpa batas dan menghabiskan max_connections.
PROBE_CACHE = 1.0
_probe_lock = asyncio.Lock()
_last_probe: tuple[float, bool] = (float("-inf"), False)   # (waktu monotonic, DB terjangkau?)


async def _db_reachable() -> bool:
    global _last_probe
    async with _probe_lock:
        checked_at, ok = _last_probe
        if time.monotonic() - checked_at < PROBE_CACHE:
            return ok
        try:
            # Connect + query dibatasi 2 detik: jawab 503 sebelum timeout healthcheck (3 detik) habis
            await asyncio.wait_for(_ping_db(), timeout=2)
            ok = True
        except Exception:
            ok = False
        _last_probe = (time.monotonic(), ok)
        return ok


@router.api_route("/readyz", methods=["GET", "HEAD"])
async def readiness():
    if not await _db_reachable():
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return {"status": "ready"}
