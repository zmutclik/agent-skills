import json
import logging
import time
import uuid
from contextvars import ContextVar

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

_RESERVED = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}

access_logger = logging.getLogger("app.access")
# Health check dipanggil tiap beberapa detik oleh orchestrator/LB: tidak dicatat agar log tidak tenggelam
QUIET_PATHS = frozenset({"/healthz", "/readyz"})


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        data.update({k: v for k, v in vars(record).items() if k not in _RESERVED})
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, default=str)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
    # uvicorn memasang handler teks sendiri sebelum app diimpor: alihkan ke root agar semua log satu format JSON.
    # Access log uvicorn dimatikan (--no-access-log, Dockerfile); penggantinya log "request" di bawah.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers.clear()
        logging.getLogger(name).propagate = True


class RequestIdMiddleware:
    """Pure ASGI middleware (tanpa overhead BaseHTTPMiddleware): request ID + satu log JSON per request.
    Log "request" (status, duration_ms) adalah bahan metrics latensi & rasio 5xx (references/deployment.md §7)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        incoming = dict(scope["headers"]).get(b"x-request-id", b"").decode("latin-1")[:64]
        request_id = incoming or uuid.uuid4().hex
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status = 500                                   # bila exception lolos sebelum header terkirim

        async def send_with_request_id(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message["headers"] = [*message.get("headers", []), (b"x-request-id", request_id.encode())]
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            if scope["path"] not in QUIET_PATHS:
                access_logger.info("request", extra={
                    "method": scope["method"], "path": scope["path"], "status": status,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                })
            request_id_var.reset(token)
