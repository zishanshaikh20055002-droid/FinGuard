"""Public authentication endpoints and cookie transport."""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, Response
from sqlalchemy.orm import Session

from app.auth.dependencies import CurrentUser
from app.auth.schemas import Credentials, LoginRequest
from app.auth.service import login, logout_session, refresh_session, register
from app.core.config import Settings, get_settings
from app.db.session import get_db_session

router = APIRouter(prefix="/auth", tags=["auth"])
DbSession = Annotated[Session, Depends(get_db_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
CsrfHeader = Annotated[str | None, Header(alias="X-CSRF-Token")]
COOKIE_NAME = "fg_refresh"
COOKIE_PATH = "/api/v1/auth"


def _set_refresh_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=settings.refresh_token_days * 86400,
        path=COOKIE_PATH,
        httponly=True,
        secure=settings.cookie_secure and not settings.local_insecure_cookie,
        samesite="lax",
    )


@router.post("/register", status_code=201)
def register_user(request: Credentials, session: DbSession, settings: SettingsDep) -> dict:
    return register(session, request, settings)


@router.post("/login")
def login_user(
    request: LoginRequest, response: Response, session: DbSession, settings: SettingsDep
) -> dict:
    credentials = login(session, request, settings)
    _set_refresh_cookie(response, credentials.refresh_token, settings)
    return {
        "access_token": credentials.access_token,
        "token_type": "bearer",
        "csrf_token": credentials.csrf_token,
        "user": credentials.user,
    }


@router.post("/refresh")
def refresh_user(
    request: Request,
    response: Response,
    csrf: CsrfHeader,
    session: DbSession,
    settings: SettingsDep,
) -> dict:
    credentials = refresh_session(session, request.cookies.get(COOKIE_NAME), csrf, settings)
    _set_refresh_cookie(response, credentials.refresh_token, settings)
    return {
        "access_token": credentials.access_token,
        "token_type": "bearer",
        "csrf_token": credentials.csrf_token,
        "user": credentials.user,
    }


@router.post("/logout")
def logout_user(
    request: Request, response: Response, csrf: CsrfHeader, user: CurrentUser, session: DbSession
) -> dict:
    logout_session(session, user.session_id, request.cookies.get(COOKIE_NAME), csrf)
    response.delete_cookie(COOKIE_NAME, path=COOKIE_PATH)
    return {"status": "logged_out"}


@router.get("/me")
def me(user: CurrentUser) -> dict:
    return {
        "user_id": str(user.id),
        "email": user.email,
        "role": user.role,
        "status": user.status,
        "created_at": user.created_at.isoformat(),
    }
