"""PostgreSQL tests refuse remote targets and never fall back to DATABASE_URL."""

import os
import threading
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text

from cinemind.config import ROOT, Settings
from cinemind.database import Database, postgres_url
from cinemind.mail import DeliveryFailed
from cinemind.main import create_app

PASSWORD = "A sufficiently long test password"


class ControlledClock:
    def __init__(self):
        self.now = datetime.now(UTC)

    def __call__(self):
        return self.now

    def advance(self, seconds: int):
        self.now += timedelta(seconds=seconds)


class RecordingMailer:
    def __init__(self):
        self._messages: list[tuple[str, str]] = []
        self.fail = False
        self.lock = threading.Lock()

    def send_verification(self, email: str, code: str) -> None:
        if self.fail:
            raise DeliveryFailed()
        with self.lock:
            self._messages.append((email, code))

    def code(self, email: str) -> str:
        with self.lock:
            return next(code for address, code in reversed(self._messages) if address == email)

    def count(self) -> int:
        return len(self._messages)


@pytest.fixture(scope="session")
def postgres():
    raw = os.environ.get("TEST_DATABASE_URL")
    if not raw:
        pytest.skip("Set a local isolated TEST_DATABASE_URL to run PostgreSQL integration tests")
    url = postgres_url(raw)
    if url.host not in {"127.0.0.1", "localhost", "::1"} or not (url.database or "").startswith("cinemind_test"):
        pytest.fail("TEST_DATABASE_URL must target a loopback database named cinemind_test*")
    settings = Settings(_env_file=None, environment="test", database_url=raw)
    db = Database(settings)
    with db.engine.begin() as connection:
        config = Config(str(ROOT / "alembic.ini"))
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    yield db
    db.close()


@pytest.fixture
def db(postgres):
    with postgres.engine.begin() as connection:
        connection.execute(text("TRUNCATE users, documents CASCADE"))
    return postgres


@pytest.fixture
def clock():
    return ControlledClock()


@pytest.fixture
def mailer():
    return RecordingMailer()


@pytest.fixture
def settings():
    return Settings(_env_file=None, environment="test", verification_secret="test-only-verification-key-at-least-32-characters",
                    auth_ip_limit=100, auth_account_limit=100)


@pytest.fixture
def app(db, settings, mailer, clock):
    return create_app(settings, db=db, mailer=mailer, clock=clock)


@pytest.fixture
def clients(app):
    with ExitStack() as stack:
        def make():
            client = stack.enter_context(TestClient(app, base_url="http://127.0.0.1:8001"))
            client.headers.update({"Origin": "http://127.0.0.1:8001", "X-CineMind-CSRF": "1"})
            return client
        yield make


@pytest.fixture
def client(clients):
    return clients()


@pytest.fixture
def make_user(clients, mailer):
    def make(name="alice"):
        client = clients()
        email = f"{name}@example.com"
        assert client.post("/auth/signup", json={"username": name, "email": email, "password": PASSWORD}).status_code == 202
        assert client.post("/auth/verify-email", json={"email": email, "code": mailer.code(email)}).status_code == 200
        response = client.post("/auth/login", json={"email": email, "password": PASSWORD})
        assert response.status_code == 200
        return client, UUID(response.json()["id"]), email
    return make
