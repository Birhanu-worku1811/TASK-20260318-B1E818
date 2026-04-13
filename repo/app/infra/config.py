from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "offline-platform-api"
    app_env: str = "dev"
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/offline_platform"
    allow_dev_sqlite_override: bool = False
    master_encryption_key: str = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
    access_token_expire_minutes: int = 120
    jwt_secret: str = "dev-secret-change-me"
    jwt_algorithm: str = "HS256"
    order_auto_void_minutes: int = 30
    account_lock_minutes: int = 15
    failed_login_limit: int = 5
    attachment_max_mb: int = 20
    receipt_printer_backend: str = "noop"
    receipt_printer_host: str = "127.0.0.1"
    receipt_printer_port: int = 9100
    receipt_printer_device_path: str = "/dev/usb/lp0"
    bootstrap_mode: bool = False
    install_bootstrap_token: str = ""
    allow_admin_reviewer_project_override: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()


def validate_runtime_settings(settings: Settings) -> None:
    database_url = settings.database_url.lower()
    is_postgresql = database_url.startswith("postgresql")
    dev_sqlite_override = settings.app_env.lower() == "dev" and settings.allow_dev_sqlite_override and database_url.startswith("sqlite")
    if not is_postgresql and not dev_sqlite_override:
        raise RuntimeError(
            "DATABASE_URL must be PostgreSQL in accepted runtime. "
            "Use APP_ENV=dev and ALLOW_DEV_SQLITE_OVERRIDE=true only for explicit local SQLite override."
        )

    # Enforce a strong HS256 key outside local dev.
    if settings.app_env.lower() != "dev" and settings.jwt_algorithm.upper() == "HS256" and len(settings.jwt_secret) < 32:
        raise RuntimeError("JWT_SECRET must be at least 32 bytes when using HS256 outside dev")
