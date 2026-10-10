from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from app.core.database import engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http_client = httpx.AsyncClient(
        limits=httpx.Limits(max_keepalive_connections=20, max_connections=100),
        timeout=httpx.Timeout(5.0, connect=2.0),
    )

    yield

    await app.state.http_client.aclose()
    await engine.dispose()
