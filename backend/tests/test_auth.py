from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

from sqlalchemy import func, select

from cinemind.models import EmailVerification, LoginSession, User
from cinemind.schemas import VerifyEmail
from cinemind.errors import ServiceError
from cinemind.security import token_hash
from conftest import PASSWORD


def signup(client, name="alice"):
    return client.post("/auth/signup", json={"username": name, "email": f"{name}@example.com", "password": PASSWORD})


def test_signup_verification_login_and_safe_profile(client, mailer, db):
    response = signup(client)
    assert response.status_code == 202
    assert "code" not in response.text
    assert client.get("/auth/me").status_code == 401
    assert client.post("/auth/login", json={"email": "alice@example.com", "password": PASSWORD}).json()["error"]["code"] == "verification_required"
    with db.sessions.begin() as session:
        user = session.scalar(select(User))
        challenge = session.scalar(select(EmailVerification))
        assert user.password_hash.startswith("$argon2id$")
        assert PASSWORD not in user.password_hash
        assert challenge.code_digest != mailer.code(user.email)
    assert client.post("/auth/verify-email", json={"email": "ALICE@example.com", "code": mailer.code("alice@example.com")}).status_code == 200
    assert client.get("/auth/me").status_code == 401
    response = client.post("/auth/login", json={"email": "ALICE@example.com", "password": PASSWORD})
    assert response.status_code == 200
    assert response.json()["email_verified_at"] is not None
    assert "password_hash" not in response.text and "token" not in response.text
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Path=/" in cookie
    token = client.cookies.get("cinemind_session")
    with db.sessions.begin() as session:
        stored = session.scalar(select(LoginSession))
        assert stored.token_hash == token_hash(token)
        assert str(stored.id) != token
    assert client.get("/auth/me").status_code == 200


def test_duplicate_signup_does_not_leak_or_replace_password(client, mailer, make_user, db):
    logged, _, _ = make_user()
    response = client.post("/auth/signup", json={"username": "different", "email": "ALICE@example.com", "password": "Another long password"})
    other = client.post("/auth/signup", json={"username": "alice", "email": "different@example.com", "password": PASSWORD})
    unknown = client.post("/auth/resend-verification", json={"email": "unknown@example.com"})
    verified = client.post("/auth/resend-verification", json={"email": "alice@example.com"})
    assert response.status_code == other.status_code == unknown.status_code == verified.status_code == 202
    assert response.json() == other.json() == unknown.json() == verified.json()
    assert mailer.count() == 1
    with db.sessions.begin() as session:
        assert session.scalar(select(func.count()).select_from(User)) == 1
    assert logged.post("/auth/login", json={"email": "alice@example.com", "password": "Another long password"}).status_code == 401
    assert logged.post("/auth/login", json={"email": "alice@example.com", "password": PASSWORD}).status_code == 200


def test_failed_and_unknown_login_are_indistinguishable(client):
    signup(client)
    known = client.post("/auth/login", json={"email": "alice@example.com", "password": "incorrect"})
    unknown = client.post("/auth/login", json={"email": "unknown@example.com", "password": "incorrect"})
    assert known.status_code == unknown.status_code == 401
    assert known.json() == unknown.json()


def test_leading_zero_code_and_reuse(client, mailer, monkeypatch):
    monkeypatch.setattr("cinemind.services.auth.secrets.randbelow", lambda maximum: 7)
    signup(client)
    assert mailer.code("alice@example.com").startswith("000")
    payload = {"email": "alice@example.com", "code": mailer.code("alice@example.com")}
    assert client.post("/auth/verify-email", json=payload).status_code == 200
    assert client.post("/auth/verify-email", json=payload).status_code == 400


def test_resend_cooldown_invalidates_previous_code(client, mailer, clock, db, monkeypatch):
    values = iter((123456, 654321))
    monkeypatch.setattr("cinemind.services.auth.secrets.randbelow", lambda maximum: next(values))
    signup(client)
    old = mailer.code("alice@example.com")
    assert client.post("/auth/resend-verification", json={"email": "alice@example.com"}).status_code == 202
    assert mailer.count() == 1
    clock.advance(61)
    assert client.post("/auth/resend-verification", json={"email": "alice@example.com"}).status_code == 202
    assert mailer.count() == 2
    assert client.post("/auth/verify-email", json={"email": "alice@example.com", "code": old}).status_code == 400
    assert client.post("/auth/verify-email", json={"email": "alice@example.com", "code": mailer.code("alice@example.com")}).status_code == 200
    with db.sessions.begin() as session:
        rows = list(session.scalars(select(EmailVerification)))
        assert sum(row.invalidated_at is not None for row in rows) == 1


def test_expired_code_is_rejected(client, mailer, clock):
    signup(client)
    clock.advance(601)
    assert client.post("/auth/verify-email", json={"email": "alice@example.com", "code": mailer.code("alice@example.com")}).status_code == 400


def test_failed_attempts_persist_and_exhaust(client, mailer, db):
    signup(client)
    wrong = "000000" if mailer.code("alice@example.com") != "000000" else "000001"
    for _ in range(5):
        assert client.post("/auth/verify-email", json={"email": "alice@example.com", "code": wrong}).status_code == 400
    with db.sessions.begin() as session:
        row = session.scalar(select(EmailVerification))
        assert row.failed_attempts == 5 and row.invalidated_at is not None
    assert client.post("/auth/verify-email", json={"email": "alice@example.com", "code": mailer.code("alice@example.com")}).status_code == 400


def test_delivery_failure_preserves_account_and_allows_immediate_resend(client, mailer, db):
    mailer.fail = True
    assert signup(client).status_code == 202
    with db.sessions.begin() as session:
        assert session.scalar(select(User)) is not None
        assert session.scalar(select(EmailVerification)).invalidated_at is not None
    mailer.fail = False
    assert client.post("/auth/resend-verification", json={"email": "alice@example.com"}).status_code == 202
    assert mailer.count() == 1


def test_expiry_and_single_session_logout(make_user, clients, clock):
    first, _, email = make_user()
    second = clients()
    assert second.post("/auth/login", json={"email": email, "password": PASSWORD}).status_code == 200
    sessions = first.get("/auth/sessions").json()
    assert len(sessions) == 2 and sum(item["is_current"] for item in sessions) == 1
    assert all("token_hash" not in item for item in sessions)
    old_token = first.cookies.get("cinemind_session")
    assert first.post("/auth/logout").status_code == 200
    assert first.post("/auth/logout").status_code == 200
    first.cookies.set("cinemind_session", old_token)
    assert first.get("/auth/me").status_code == 401
    assert second.get("/auth/me").status_code == 200
    clock.advance(604801)
    assert second.get("/auth/me").status_code == 401


def test_session_management_is_owner_scoped(make_user, clients):
    alice, _, email = make_user()
    bob, _, _ = make_user("bob")
    second = clients()
    second.post("/auth/login", json={"email": email, "password": PASSWORD})
    first_id = next(item["id"] for item in alice.get("/auth/sessions").json() if item["is_current"])
    assert bob.delete(f"/auth/sessions/{first_id}").status_code == 404
    assert second.delete(f"/auth/sessions/{first_id}").status_code == 204
    assert alice.get("/auth/me").status_code == 401
    assert second.get("/auth/me").status_code == 200
    second_id = next(item["id"] for item in second.get("/auth/sessions").json() if item["is_current"])
    response = second.delete(f"/auth/sessions/{second_id}")
    assert response.status_code == 204
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert second.get("/auth/me").status_code == 401


def test_concurrent_verification_consumes_once(client, mailer, app):
    signup(client)
    data = VerifyEmail(email="alice@example.com", code=mailer.code("alice@example.com"))
    def attempt(_):
        try:
            app.state.auth.verify(data, "same-client")
            return 200
        except ServiceError as error:
            return error.status
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, range(2))) == [200, 400]


def test_concurrent_resend_creates_one_challenge(client, mailer, app, clock, db):
    signup(client)
    clock.advance(61)
    app.state.auth.limiter.settings.delivery_account_limit = 100
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: app.state.auth.resend("alice@example.com", "same-client"), range(4)))
    assert mailer.count() == 2
    with db.sessions.begin() as session:
        assert session.scalar(select(func.count()).select_from(EmailVerification).where(
            EmailVerification.used_at.is_(None), EmailVerification.invalidated_at.is_(None))) == 1


def test_concurrent_wrong_attempts_cannot_exceed_cap(client, mailer, app, db):
    signup(client)
    wrong = "000000" if mailer.code("alice@example.com") != "000000" else "000001"
    data = VerifyEmail(email="alice@example.com", code=wrong)
    def attempt(_):
        try:
            app.state.auth.verify(data, "same-client")
        except ServiceError as error:
            return error.status
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert set(pool.map(attempt, range(12))) == {400}
    with db.sessions.begin() as session:
        assert session.scalar(select(EmailVerification)).failed_attempts == 5


def test_auth_rate_limits(client, app, clock):
    app.state.auth.limiter.settings.auth_account_limit = 2
    for _ in range(2):
        assert client.post("/auth/login", json={"email": "unknown@example.com", "password": "wrong"}).status_code == 401
    assert client.post("/auth/login", json={"email": "unknown@example.com", "password": "wrong"}).status_code == 429
    for _ in range(3):
        assert client.post("/auth/resend-verification", json={"email": "unknown@example.com"}).status_code == 202
    assert client.post("/auth/resend-verification", json={"email": "unknown@example.com"}).status_code == 429
    clock.advance(61)
    assert client.post("/auth/login", json={"email": "unknown@example.com", "password": "wrong"}).status_code == 401
