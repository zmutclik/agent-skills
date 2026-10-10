"""Menjalankan worker asli sebagai proses terpisah dengan task test terdaftar (dipakai test SIGTERM)."""
import tests.support_tasks  # noqa: F401
from app.jobs.worker import main

if __name__ == "__main__":
    main()
