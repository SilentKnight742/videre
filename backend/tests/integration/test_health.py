from fastapi.testclient import TestClient

from videre.config import Settings


def test_health_ok(client: TestClient) -> None:
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok", "version": "0.1.0", "rail": "mock", "demo_replay": False}


def test_startup_creates_the_database(client: TestClient, settings: Settings) -> None:
    assert settings.db_path.exists()


def test_cors_allows_only_the_frontend_origin(client: TestClient, settings: Settings) -> None:
    ok = client.get("/api/health", headers={"Origin": settings.frontend_origin})
    assert ok.headers["access-control-allow-origin"] == settings.frontend_origin
    other = client.get("/api/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in other.headers
