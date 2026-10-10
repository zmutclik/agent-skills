from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from app.core import health
from app.core.config import settings
from app.core.discovery import APP_DIR, import_domain_modules
from app.core.errors import UnhandledErrorMiddleware, install_error_handlers
from app.core.lifespan import lifespan
from app.core.logging import RequestIdMiddleware, setup_logging
from app.core.middleware import SecurityHeadersMiddleware


def create_app() -> FastAPI:
    setup_logging()
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    app.add_middleware(UnhandledErrorMiddleware)   # paling dalam: 500 tetap membawa security headers & request ID
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestIdMiddleware)      # paling luar: request ID tersedia untuk semua layer
    install_error_handlers(app)

    app.include_router(health.router)
    for module in import_domain_modules("router"):   # router domain ditemukan otomatis
        app.include_router(module.router)

    @app.get("/", include_in_schema=False)
    async def index():
        return RedirectResponse(settings.HOME_PATH, status_code=303)

    if settings.DEBUG:   # production: static di-serve reverse proxy
        from fastapi.staticfiles import StaticFiles
        app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")

    return app


app = create_app()
