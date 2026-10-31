"""Lazy synchronous database engine and connectivity probe."""

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import get_settings


@lru_cache
def get_engine(database_url: str) -> Engine:
    return create_engine(
        database_url,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={"connect_timeout": 3},
    )


def check_database_connection() -> bool:
    database_url = get_settings().database_url
    if not database_url:
        return False
    try:
        with get_engine(database_url).connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return False
    return True


def get_db_session() -> Iterator[Session]:
    database_url = get_settings().database_url
    if not database_url:
        raise RuntimeError("Database is not configured")
    with Session(get_engine(database_url), expire_on_commit=False) as session:
        yield session
