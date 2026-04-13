from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class APIError(Exception):
    def __init__(self, *, status_code: int, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(message)


def success(data: Any = None, message: str = "ok") -> dict[str, Any]:
    return {"status": "success", "message": message, "data": data}


def _sanitize_validation_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sensitive_markers = {"password", "token", "secret", "id_number", "contact"}
    sanitized: list[dict[str, Any]] = []
    for err in errors:
        cleaned = dict(err)
        loc_parts = [str(p).lower() for p in cleaned.get("loc", [])]
        if any(any(marker in part for marker in sensitive_markers) for part in loc_parts):
            if "input" in cleaned:
                cleaned["input"] = "***REDACTED***"
            cleaned["msg"] = "Invalid sensitive field value"
        sanitized.append(cleaned)
    return sanitized


def install_exception_handlers(app: FastAPI) -> None:
    logger = logging.getLogger("offline_platform_api")

    @app.exception_handler(APIError)
    async def _api_error_handler(_: Request, exc: APIError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "status": "error",
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
            },
        )

    @app.exception_handler(HTTPException)
    async def _http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "status": "error",
                "code": "http_error",
                "message": str(exc.detail),
                "details": {},
            },
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "status": "error",
                "code": "validation_error",
                "message": "Request validation failed",
                "details": {"errors": _sanitize_validation_errors(exc.errors())},
            },
        )

    @app.exception_handler(Exception)
    async def _generic_handler(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled server exception", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "code": "internal_error",
                "message": "Internal server error",
                "details": {},
            },
        )
