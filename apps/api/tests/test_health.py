from fastapi.testclient import TestClient

from codemind.core.config import settings
from codemind.main import app

client = TestClient(app)


def test_root_returns_app_info() -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert response.json() == {
        "name": settings.app_name,
        "version": settings.app_version,
        "status": "running",
    }


def test_health_returns_healthy() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}
