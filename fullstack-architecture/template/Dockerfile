FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/srv/.venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv

WORKDIR /srv

# Layer dependency terpisah dari kode: rebuild cepat saat hanya kode berubah
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app

RUN useradd --system --no-create-home app
USER app

EXPOSE 8000

# Satu image untuk web, migrasi (`alembic upgrade head`), cleanup terjadwal (`python -m app.auth.cleanup`),
# dan worker bila addon jobs dipasang (`python -m app.jobs.worker`).
# WEB_CONCURRENCY: mulai dari jumlah core; perhitungkan di rumus koneksi DB.
# --timeout-keep-alive 75 > keepalive_timeout upstream nginx (60 s): yang menutup koneksi idle selalu nginx,
# bukan uvicorn (default uvicorn 5 s → 502 sesekali pada POST).
# --no-access-log: access log teks uvicorn diganti log JSON "request" ber-request_id (app/core/logging.py).
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers ${WEB_CONCURRENCY:-2} --proxy-headers --forwarded-allow-ips=${FORWARDED_ALLOW_IPS:-127.0.0.1} --timeout-keep-alive 75 --no-access-log --no-server-header"]
