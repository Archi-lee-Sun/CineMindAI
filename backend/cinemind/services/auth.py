"""Authentication owns credential writes, challenges and server-side sessions."""

import hmac
import re
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta
from functools import partial
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cinemind.config import Settings
from cinemind.database import Clock, Database, utcnow
from cinemind.errors import ServiceError, not_found
from cinemind.mail import DeliveryFailed, Mailer
from cinemind.models import EmailVerification, LoginSession, User
from cinemind.schemas import Login, Profile, SessionView, Signup, VerifyEmail
from cinemind.security import CurrentUser, Passwords, RateLimiter, code_digest, token_hash

GENERIC_DELIVERY = "If the account is eligible, a verification email will be sent."
INVALID_CODE = "The verification code is invalid or expired."
DeliveryScheduler = Callable[[Callable[[], None]], None]


class AuthService:
    def __init__(self, db: Database, settings: Settings, mailer: Mailer,
                 clock: Clock = utcnow, limiter: RateLimiter | None = None):
        self.db, self.settings, self.mailer, self.clock = db, settings, mailer, clock
        self.passwords = Passwords()
        self.limiter = limiter or RateLimiter(settings)

    def _secret(self) -> str:
        if not self.settings.verification_secret:
            raise ServiceError(503, "verification_unavailable", "Email verification is not configured.")
        return self.settings.verification_secret.get_secret_value()

    def _issue(self, session: Session, user: User, now: datetime) -> tuple[UUID, str, str]:
        # The caller holds the user's row lock (or just inserted that user).
        session.execute(update(EmailVerification).where(
            EmailVerification.user_id == user.id, EmailVerification.used_at.is_(None),
            EmailVerification.invalidated_at.is_(None),
        ).values(invalidated_at=now))
        session.flush()
        challenge_id = uuid4()
        code = f"{secrets.randbelow(1000000):06d}"
        session.add(EmailVerification(
            id=challenge_id, user_id=user.id, email=user.email,
            code_digest=code_digest(self._secret(), challenge_id, user.email, code),
            expires_at=now + timedelta(seconds=self.settings.verification_ttl_seconds),
            created_at=now, failed_attempts=0,
        ))
        return challenge_id, user.email, code

    def _deliver(self, challenge: tuple[UUID, str, str] | None) -> None:
        if challenge is None:
            return
        challenge_id, email, code = challenge
        # SMTP is outside the issuance transaction. Failure allows immediate resend.
        try:
            self.mailer.send_verification(email, code)
        except DeliveryFailed:
            with self.db.sessions.begin() as session:
                session.execute(update(EmailVerification).where(
                    EmailVerification.id == challenge_id, EmailVerification.used_at.is_(None),
                    EmailVerification.invalidated_at.is_(None),
                ).values(invalidated_at=self.clock()))

    def _schedule_delivery(self, challenge: tuple[UUID, str, str] | None, defer: DeliveryScheduler | None) -> None:
        if challenge is not None:
            if defer is None:
                self._deliver(challenge)
            else:
                defer(partial(self._deliver, challenge))

    def signup(self, data: Signup, client: str, *, defer: DeliveryScheduler | None = None) -> None:
        self.db.require()
        self._secret()
        now = self.clock()
        self.limiter.check("delivery", data.email, client, now)
        password_hash = self.passwords.hash(data.password)
        challenge = None
        try:
            with self.db.sessions.begin() as session:
                user = User(username=data.username, email=data.email, password_hash=password_hash,
                            created_at=now, updated_at=now)
                session.add(user)
                session.flush()
                challenge = self._issue(session, user, now)
        except IntegrityError:
            # Never overwrite a duplicate account or reveal which field exists.
            return
        self._schedule_delivery(challenge, defer)

    def resend(self, email: str, client: str, *, defer: DeliveryScheduler | None = None) -> None:
        self.db.require()
        self._secret()
        now = self.clock()
        self.limiter.check("delivery", email, client, now)
        challenge = None
        with self.db.sessions.begin() as session:
            user = session.scalar(select(User).where(func.lower(User.email) == email.lower()).with_for_update())
            if user is not None and user.email_verified_at is None:
                previous = session.scalar(select(EmailVerification).where(
                    EmailVerification.user_id == user.id,
                    EmailVerification.used_at.is_(None), EmailVerification.invalidated_at.is_(None),
                ))
                if previous is None or (now - previous.created_at).total_seconds() >= self.settings.verification_resend_cooldown_seconds:
                    challenge = self._issue(session, user, now)
        self._schedule_delivery(challenge, defer)

    def verify(self, data: VerifyEmail, client: str) -> None:
        self.db.require()
        secret = self._secret()
        now = self.clock()
        self.limiter.check("verify", data.email, client, now)
        valid = False
        with self.db.sessions.begin() as session:
            user = session.scalar(select(User).where(func.lower(User.email) == data.email).with_for_update())
            now = self.clock()
            if user is not None and user.email_verified_at is None:
                challenge = session.scalar(select(EmailVerification).where(
                    EmailVerification.user_id == user.id, EmailVerification.used_at.is_(None),
                    EmailVerification.invalidated_at.is_(None),
                ).with_for_update())
                if challenge is not None:
                    if challenge.expires_at <= now or challenge.failed_attempts >= self.settings.verification_max_attempts:
                        challenge.invalidated_at = now
                    elif hmac.compare_digest(challenge.code_digest, code_digest(secret, challenge.id, data.email, data.code)):
                        challenge.used_at = now
                        user.email_verified_at = now
                        user.updated_at = now
                        valid = True
                    else:
                        challenge.failed_attempts += 1
                        if challenge.failed_attempts >= self.settings.verification_max_attempts:
                            challenge.invalidated_at = now
        # Raise only AFTER commit: wrong-attempt counters must survive the error.
        if not valid:
            raise ServiceError(400, "invalid_verification", INVALID_CODE)

    def login(self, data: Login, client: str) -> tuple[Profile, str]:
        self.db.require()
        now = self.clock()
        self.limiter.check("login", data.email, client, now)
        with self.db.sessions.begin() as session:
            user = session.scalar(select(User).where(func.lower(User.email) == data.email))
            stored = user.password_hash if user else None
            user_id = user.id if user else None
        if not self.passwords.matches(stored, data.password):
            raise ServiceError(401, "invalid_credentials", "Invalid email or password.")
        token = secrets.token_urlsafe(32)
        with self.db.sessions.begin() as session:
            user = session.scalar(select(User).where(User.id == user_id).with_for_update())
            if user is None or user.password_hash != stored:
                raise ServiceError(401, "invalid_credentials", "Invalid email or password.")
            if user.email_verified_at is None:
                raise ServiceError(403, "verification_required", "Verify your email before logging in.")
            session.add(LoginSession(user_id=user.id, token_hash=token_hash(token), created_at=now,
                                     expires_at=now + timedelta(seconds=self.settings.session_ttl_seconds)))
            profile = Profile.model_validate(user)
        return profile, token

    def current(self, token: str | None) -> CurrentUser:
        if not token or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            raise ServiceError(401, "authentication_required", "Authentication required.")
        self.db.require()
        with self.db.sessions.begin() as session:
            row = session.execute(select(LoginSession.id, LoginSession.user_id).join(User).where(
                LoginSession.token_hash == token_hash(token), LoginSession.revoked_at.is_(None),
                LoginSession.expires_at > self.clock(), User.email_verified_at.is_not(None),
            )).one_or_none()
        if row is None:
            raise ServiceError(401, "authentication_required", "Authentication required.")
        return CurrentUser(user_id=row.user_id, session_id=row.id)

    def logout(self, token: str | None) -> None:
        if token and re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            self.db.require()
            with self.db.sessions.begin() as session:
                session.execute(update(LoginSession).where(
                    LoginSession.token_hash == token_hash(token), LoginSession.revoked_at.is_(None),
                ).values(revoked_at=self.clock()))

    def profile(self, identity: CurrentUser) -> Profile:
        with self.db.sessions.begin() as session:
            user = session.get(User, identity.user_id)
            if user is None:
                raise not_found()
            return Profile.model_validate(user)

    def sessions(self, identity: CurrentUser, limit: int = 50, offset: int = 0) -> list[SessionView]:
        from cinemind.services.access import pagination
        pagination(limit, offset)
        with self.db.sessions.begin() as session:
            rows = session.scalars(select(LoginSession).where(LoginSession.user_id == identity.user_id)
                                   .order_by(LoginSession.created_at.desc(), LoginSession.id).limit(limit).offset(offset))
            return [SessionView(id=row.id, created_at=row.created_at, expires_at=row.expires_at,
                                revoked_at=row.revoked_at, is_current=row.id == identity.session_id) for row in rows]

    def revoke_session(self, identity: CurrentUser, session_id: UUID) -> bool:
        with self.db.sessions.begin() as session:
            row = session.scalar(select(LoginSession).where(
                LoginSession.id == session_id, LoginSession.user_id == identity.user_id,
            ).with_for_update())
            if row is None:
                raise not_found()
            row.revoked_at = row.revoked_at or self.clock()
        return session_id == identity.session_id

    def delete_account(self, user_id: UUID) -> None:
        """Trusted internal operation; there is no public account-deletion route."""
        self.db.require()
        with self.db.sessions.begin() as session:
            user = session.get(User, user_id)
            if user is None:
                raise not_found()
            session.delete(user)
