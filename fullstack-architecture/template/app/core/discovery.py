import importlib
from pathlib import Path
from types import ModuleType

APP_DIR = Path(__file__).resolve().parent.parent


def import_domain_modules(module: str) -> list[ModuleType]:
    """Impor app/<domain>/<module>.py untuk semua domain, mis. "models", "tasks", "router"."""
    return [
        importlib.import_module(f"app.{path.parent.name}.{module}")
        for path in sorted(APP_DIR.glob(f"*/{module}.py"))
    ]
