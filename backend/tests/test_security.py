import socket
import threading

import pytest
from aiosmtpd.controller import Controller
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError

from cinemind.config import Settings
from cinemind.database import Database, postgres_url
from cinemind.mail import SMTPMailer
from cinemind.main import create_app


@pytest.mark.parametrize("headers", [
    {}, {"Origin": "http://127.0.0.1:8001"}, {"X-CineMind-CSRF": "1"},
    {"Origin": "null", "X-CineMind-CSRF": "1"},
    {"Origin": "https://evil.example", "X-CineMind-CSRF": "1"},
    {"Origin": "http://127.0.0.1:8001.evil.example", "X-CineMind-CSRF": "1"},
])
def test_csrf_rejects_unsafe_requests(settings, headers):
    with TestClient(create_app(settings)) as client:
        for path in ("/auth/login", "/auth/logout", "/auth/signup", "/conversations"):
            response = client.post(path, json={}, headers=headers)
            assert response.status_code == 403
            assert response.json()["error"]["code"] == "csrf_rejected"


def test_unauthenticated_requests_and_validation_do_not_echo_secrets(client):
    assert client.get("/conversations").status_code == 401
    assert client.post("/conversations", json={}).status_code == 401
    response = client.post("/auth/signup", json={"username": "a", "email": "bad", "password": "a-secret"})
    assert response.status_code == 422
    assert "a-secret" not in response.text
    assert response.json() == {"error": {"code": "validation_error", "message": "Request validation failed."}}


def test_cors_uses_exact_origins(settings):
    with TestClient(create_app(settings)) as client:
        allowed = client.options("/auth/login", headers={"Origin": "http://127.0.0.1:8001",
            "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "X-CineMind-CSRF,Content-Type"})
        assert allowed.status_code == 200
        assert allowed.headers["access-control-allow-origin"] == "http://127.0.0.1:8001"
        assert allowed.headers["access-control-allow-credentials"] == "true"
        denied = client.options("/auth/login", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
        assert denied.status_code == 400
        assert "access-control-allow-origin" not in denied.headers


def test_readiness_is_separate_and_generic():
    settings = Settings(_env_file=None)
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/conversations").status_code == 401
        response = client.get("/ready")
        assert response.status_code == 503
        assert response.json()["error"]["message"] == "Database unavailable."


def test_readiness_handles_connection_failure(settings, monkeypatch):
    from pydantic import SecretStr
    settings.database_url = SecretStr("postgresql://fake@127.0.0.1:1/unreachable")
    db = Database(settings)
    def fail():
        raise OperationalError("sensitive-uri", {}, Exception("sensitive-password"))
    monkeypatch.setattr(db.engine, "connect", fail)
    with TestClient(create_app(settings, db=db)) as client:
        response = client.get("/ready")
        assert response.status_code == 503 and "sensitive" not in response.text
        assert client.get("/health").status_code == 200
    db.close()


def test_url_normalization_preserves_everything_except_driver():
    from sqlalchemy.engine import make_url
    raw = "postgresql://name:p%40ss@ep-example-pooler.neon.tech/neondb?sslmode=require&channel_binding=require&application_name=a%20b"
    original = make_url(raw)
    normalized = postgres_url(raw)
    assert normalized == original.set(drivername="postgresql+psycopg")


def test_environment_overrides_dotenv(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("DATABASE_URL=postgresql://dotenv@localhost/from_dotenv\n", encoding="utf-8")
    monkeypatch.setenv("DATABASE_URL", "postgresql://process@localhost/from_process")
    settings = Settings(_env_file=path)
    assert postgres_url(settings.database_url.get_secret_value()).database == "from_process"


@pytest.mark.parametrize("kwargs", [
    {"environment": "production"},
    {"session_cookie_samesite": "none", "session_cookie_secure": False},
    {"trusted_origins": ["*"]}, {"trusted_origins": ["https://example.com/path"]},
    {"verification_secret": "short"},
    {"verification_secret": "REPLACE_WITH_A_RANDOM_SECRET_OF_AT_LEAST_32_CHARACTERS"},
])
def test_insecure_configuration_is_rejected(kwargs):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **kwargs)


def test_insecure_development_cookie_refuses_non_loopback(settings):
    settings.environment = "development"
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8001", client=("203.0.113.1", 50000)) as client:
        response = client.post("/auth/logout", headers={"Origin": "http://127.0.0.1:8001", "X-CineMind-CSRF": "1"})
        assert response.status_code == 403 and response.json()["error"]["code"] == "https_required"


def test_smtp_delivery_captured_only_by_local_inbox():
    class Inbox:
        def __init__(self):
            self.delivered = threading.Event()
            self.content = b""

        async def handle_DATA(self, server, session, envelope):
            self.content = envelope.content
            self.delivered.set()
            return "250 OK"

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    inbox = Inbox()
    controller = Controller(inbox, hostname="127.0.0.1", port=port)
    controller.start()
    try:
        SMTPMailer(Settings(_env_file=None, smtp_port=port)).send_verification("capture@example.com", "001234")
        assert inbox.delivered.wait(2)
        assert b"001234" in inbox.content
        assert b"capture@example.com" in inbox.content
    finally:
        controller.stop()


def test_secure_cookie_and_clear_attributes(db, settings, mailer, clock):
    settings.session_cookie_secure = True
    settings.trusted_origins = ["https://api.example.com"]
    app = create_app(settings, db=db, mailer=mailer, clock=clock)
    headers = {"Origin": "https://api.example.com", "X-CineMind-CSRF": "1"}
    with TestClient(app, base_url="https://api.example.com", headers=headers) as client:
        password = "A long enough test password"
        client.post("/auth/signup", json={"username": "alice", "email": "alice@example.com", "password": password})
        client.post("/auth/verify-email", json={"email": "alice@example.com", "code": mailer.code("alice@example.com")})
        login = client.post("/auth/login", json={"email": "alice@example.com", "password": password})
        assert login.status_code == 200 and "Secure" in login.headers["set-cookie"]
        logout = client.post("/auth/logout")
        cookie = logout.headers["set-cookie"]
        assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie and "Path=/" in cookie
