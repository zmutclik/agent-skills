import httpx
from fastapi import Request


def get_http_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.http_client


def client_ip(request: Request) -> str:
    # Benar hanya bila uvicorn dijalankan dengan --proxy-headers --forwarded-allow-ips=<IP proxy>
    return request.client.host if request.client else "unknown"
