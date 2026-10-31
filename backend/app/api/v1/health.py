"""Liveness and database readiness endpoints."""

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse

from app.db.session import check_database_connection

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
def live() -> dict[str, str]:
    return {"status": "alive"}


@router.get("/ready")
def ready(database_available: bool = Depends(check_database_connection)) -> JSONResponse:
    if not database_available:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "unavailable"},
        )
    return JSONResponse(content={"status": "ready"})
