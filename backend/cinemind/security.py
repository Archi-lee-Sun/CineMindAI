"""Argon2id credentials, opaque session hashes and bounded development limits."""

import hashlib
import hmac
import secrets
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from functools import cached_property
from uuid import UUID

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError

from cinemind.config import Settings
from cinemind.errors import ServiceError


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def code_digest(secret: str, challenge_id: UUID, email: str, code: str) -> str:
    return hmac.new(secret.encode(), f"{challenge_id}|{email}|{code}".encode(), hashlib.sha256).hexdigest()


class Passwords:
    def __init__(self):
        self.hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1, type=Type.ID)

    @cached_property
    def dummy(self) -> str:
        # First computed by a sync auth operation in a worker, never at app import.
        return self.hash(secrets.token_urlsafe(32))

    def hash(self, password: str) -> str:
        return self.hasher.hash(password)

    def matches(self, stored: str | None, password: str) -> bool:
        try:
            return self.hasher.verify(stored or self.dummy, password) and stored is not None
        except (VerificationError, InvalidHashError):
            return False


@dataclass(frozen=True)
class CurrentUser:
    user_id: UUID
    session_id: UUID


class RateLimiter:
    """One-process development limiter. Never stores raw addresses or client IPs."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._key = secrets.token_bytes(32)
        self._events: dict[bytes, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, action: str, address: str, client: str, now: datetime) -> None:
        window = self.settings.auth_rate_window_seconds
        account_limit = self.settings.delivery_account_limit if action == "delivery" else self.settings.auth_account_limit
        signals = ((f"{action}:address:{address.lower()}", account_limit),
                   (f"{action}:client:{client}", self.settings.auth_ip_limit))
        timestamp = now.timestamp()
        with self._lock:
            for key in list(self._events):
                queue = self._events[key]
                while queue and queue[0] <= timestamp - window:
                    queue.popleft()
                if not queue:
                    del self._events[key]
            if len(self._events) > 10000:
                raise ServiceError(429, "rate_limited", "Try again later.")
            queues = []
            for signal, limit in signals:
                key = hmac.digest(self._key, signal.encode(), "sha256")
                queue = self._events.setdefault(key, deque())
                if len(queue) >= limit:
                    raise ServiceError(429, "rate_limited", "Try again later.")
                queues.append(queue)
            for queue in queues:
                queue.append(timestamp)
