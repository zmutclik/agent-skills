from pydantic_settings import BaseSettings, SettingsConfigDict


class JobSettings(BaseSettings):
    """Setting milik addon jobs; dibaca dari .env yang sama dengan app/core/config.py."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    WORKER_CONCURRENCY: int = 10
    WORKER_POLL_INTERVAL: float = 1.0
    WORKER_SHUTDOWN_GRACE: float = 25    # < stop_grace_period / terminationGracePeriodSeconds
    JOB_TIMEOUT: int = 300
    DB_JOB_STATEMENT_TIMEOUT: float = 60  # engine worker; engine web tetap DB_STATEMENT_TIMEOUT


job_settings = JobSettings()
