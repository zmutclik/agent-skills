"""Security headers + Cache-Control default. HSTS dipasang di reverse proxy (hanya berlaku di HTTPS).
Static production di-serve nginx (tidak lewat middleware ini), jadi no-store tidak berlaku untuk static."""

# script-src 'self' tanpa 'unsafe-eval' / 'unsafe-inline': wajib memakai build CSP Alpine
# (@alpinejs/csp), htmx allowEval=false, dan tanpa <script>/style inline di template.
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)

SECURITY_HEADERS = [
    (b"content-security-policy", CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
]


class SecurityHeadersMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = [*message.get("headers", []), *SECURITY_HEADERS]
                # Respons app berisi data per user: default no-store agar tombol back setelah logout atau proxy
                # tidak menampilkan halaman orang lain. Endpoint yang aman di-cache memasang Cache-Control sendiri.
                if not any(name.lower() == b"cache-control" for name, _ in headers):
                    headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)
