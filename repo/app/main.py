from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from sqlalchemy import text

from app.api.v1.routes import router as v1_router
from app.domain.services import auto_void_unsettled_orders, compact_feature_values
from app.infra.config import get_settings, validate_runtime_settings
from app.infra.db import Base, SessionLocal, engine
from app.infra.response import install_exception_handlers, success


def ensure_audit_immutability() -> None:
    settings = get_settings()
    with engine.begin() as conn:
        dialect = conn.dialect.name
        if dialect != "postgresql":
            if settings.app_env.lower() == "dev" and settings.allow_dev_sqlite_override:
                return
            raise RuntimeError("Accepted runtime requires PostgreSQL for immutable audit trigger guarantees")
        conn.execute(
            text(
                """
                CREATE OR REPLACE FUNCTION prevent_audit_modification()
                RETURNS TRIGGER AS $$
                BEGIN
                    RAISE EXCEPTION 'audit_logs is immutable';
                END;
                $$ LANGUAGE plpgsql;
                """
            )
        )
        conn.execute(
            text(
                """
                DROP TRIGGER IF EXISTS trg_prevent_audit_update ON audit_logs;
                CREATE TRIGGER trg_prevent_audit_update
                BEFORE UPDATE OR DELETE ON audit_logs
                FOR EACH ROW EXECUTE FUNCTION prevent_audit_modification();
                """
            )
        )


async def maintenance_loop() -> None:
    while True:
        with SessionLocal() as db:
            auto_void_unsettled_orders(db)
            compact_feature_values(db)
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(_: FastAPI):
    validate_runtime_settings(get_settings())
    Base.metadata.create_all(bind=engine)
    ensure_audit_immutability()
    task = asyncio.create_task(maintenance_loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(
    title="Offline Platform API",
    description="Offline Retail Checkout and Entrepreneurship Project Incubation Operation Middle Platform API",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)
app.include_router(v1_router)
install_exception_handlers(app)


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/swagger", include_in_schema=False)
def swagger_alias() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health")
def health() -> dict[str, str]:
    return success({"status": "ok"})
