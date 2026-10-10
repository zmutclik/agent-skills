"""Task khusus test. Didaftarkan dengan mengimpor modul ini (worker produksi tidak melihatnya)."""
import asyncio
import random
import time
from collections import Counter

from app.jobs.queue import report_progress, task

executions: Counter[int] = Counter()


@task("test.record")
async def record(ctx: dict, n: int) -> None:
    await asyncio.sleep(random.random() / 200)
    executions[n] += 1


@task("test.fail")
async def fail(ctx: dict) -> None:
    raise ValueError("selalu gagal")


@task("test.call_api")
async def call_api(ctx: dict, path: str) -> None:
    (await ctx["http"].post(f"https://api.example.invalid{path}")).raise_for_status()


@task("test.sleep")
async def sleep(ctx: dict, seconds: float) -> None:
    await report_progress(ctx, 10)
    await asyncio.sleep(seconds)


@task("test.block_loop")
async def block_loop(ctx: dict, seconds: float) -> None:
    time.sleep(seconds)                               # BUG yang disengaja: kode sync memblok event loop
