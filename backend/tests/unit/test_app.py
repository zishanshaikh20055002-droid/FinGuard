"""The app must import and answer liveness without a database."""

from fastapi.testclient import TestClient

from app.db.session import check_database_connection
from app.main import app


def test_application_import_and_liveness() -> None:
    assert app.title == "FinGuard"
    response = TestClient(app).get("/api/v1/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_readiness_reports_database_available() -> None:
    app.dependency_overrides[check_database_connection] = lambda: True
    try:
        response = TestClient(app).get("/api/v1/health/ready")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_readiness_hides_database_failure_details() -> None:
    app.dependency_overrides[check_database_connection] = lambda: False
    try:
        response = TestClient(app).get("/api/v1/health/ready")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
