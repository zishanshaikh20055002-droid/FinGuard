"""ASGI application entry point."""

import logging
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from app.api.v1.accounts import router as accounts_router
from app.api.v1.health import router as health_router
from app.api.v1.transactions import router as transactions_router
from app.auth.admin_routes import router as admin_router
from app.auth.routes import router as auth_router
from app.core.config import get_settings
from app.core.errors import DomainError
from app.events.admin import router as event_admin_router
from app.features.routes import router as feature_router

logger = logging.getLogger(__name__)

app = FastAPI(title="FinGuard", version="0.1.0")
settings = get_settings()
origins = [origin.strip() for origin in settings.allowed_origins.split(",") if origin.strip()]
hosts = [host.strip() for host in settings.trusted_hosts.split(",") if host.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=bool(origins),
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-CSRF-Token"],
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)
app.include_router(health_router, prefix="/api/v1")
app.include_router(auth_router, prefix="/api/v1")
app.include_router(admin_router, prefix="/api/v1")
app.include_router(event_admin_router, prefix="/api/v1")
app.include_router(feature_router, prefix="/api/v1")
app.include_router(accounts_router, prefix="/api/v1")
app.include_router(transactions_router, prefix="/api/v1")


@app.middleware("http")
async def correlation_id(request: Request, call_next):
    request.state.correlation_id = str(uuid4())
    response = await call_next(request)
    response.headers["X-Correlation-ID"] = request.state.correlation_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    if request.url.path.startswith("/api/v1/auth"):
        response.headers["Cache-Control"] = "no-store"
    logger.info(
        "http_request correlation_id=%s method=%s path=%s status=%s",
        request.state.correlation_id,
        request.method,
        request.url.path,
        response.status_code,
    )
    return response


@app.exception_handler(DomainError)
async def domain_error(request: Request, error: DomainError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status_code,
        content={"error": {"code": error.code, "message": error.message}},
    )


@app.exception_handler(SQLAlchemyError)
async def database_error(request: Request, error: SQLAlchemyError) -> JSONResponse:
    logger.error("database_operation_failed correlation_id=%s", request.state.correlation_id)
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "database_error", "message": "Database operation failed"}},
    )
