"""Configuration: process environment overrides the repository-root .env."""

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", env_file_encoding="utf-8-sig", extra="ignore",
        hide_input_in_errors=True,
    )

    environment: Literal["development", "test", "production"] = "development"
    database_url: SecretStr | None = None
    migration_database_url: SecretStr | None = None
    db_connect_timeout_seconds: int = Field(default=5, ge=2, le=30)
    db_statement_timeout_ms: int = Field(default=5000, ge=100, le=30000)
    db_lock_timeout_ms: int = Field(default=3000, ge=100, le=10000)
    verification_secret: SecretStr | None = None
    verification_ttl_seconds: int = Field(default=600, ge=30, le=3600)
    verification_max_attempts: int = Field(default=5, ge=1, le=20)
    verification_resend_cooldown_seconds: int = Field(default=60, ge=1, le=3600)
    session_ttl_seconds: int = Field(default=604800, ge=60, le=2592000)
    session_cookie_name: str = "cinemind_session"
    session_cookie_secure: bool = False
    session_cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    trusted_origins: list[str] = ["http://127.0.0.1:8001", "http://localhost:8001"]
    smtp_host: str = "127.0.0.1"
    smtp_port: int = Field(default=1025, ge=1, le=65535)
    smtp_tls: Literal["none", "starttls", "ssl"] = "none"
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from: str = "CineMind AI <noreply@cinemind.example>"
    smtp_timeout_seconds: int = Field(default=5, ge=1, le=30)
    auth_rate_window_seconds: int = Field(default=60, ge=1, le=3600)
    auth_ip_limit: int = Field(default=30, ge=1, le=1000)
    auth_account_limit: int = Field(default=10, ge=1, le=1000)
    delivery_account_limit: int = Field(default=3, ge=1, le=100)

    @model_validator(mode="after")
    def validate_security(self) -> "Settings":
        if not self.trusted_origins:
            raise ValueError("At least one exact trusted origin is required")
        for origin in self.trusted_origins:
            url = urlsplit(origin)
            if (url.scheme not in {"http", "https"} or not url.hostname
                    or url.path or url.query or url.fragment or url.username
                    or "*" in origin or origin.endswith("/")):
                raise ValueError("Trusted origins must be exact HTTP(S) origins")
        if self.session_cookie_samesite == "none" and not self.session_cookie_secure:
            raise ValueError("SameSite=None requires Secure cookies")
        if self.session_cookie_name.startswith("__Host-") and not self.session_cookie_secure:
            raise ValueError("__Host- cookies require Secure")
        if self.verification_secret:
            key = self.verification_secret.get_secret_value()
            if len(key) < 32 or key.startswith(("REPLACE_", "YOUR_")):
                raise ValueError("VERIFICATION_SECRET must be a unique random secret of at least 32 characters")
        if bool(self.smtp_username) != bool(self.smtp_password):
            raise ValueError("Configure both SMTP_USERNAME and SMTP_PASSWORD")
        if self.environment == "production":
            if not self.session_cookie_secure or not self.verification_secret:
                raise ValueError("Production requires Secure cookies and VERIFICATION_SECRET")
            if self.smtp_tls == "none":
                raise ValueError("Production SMTP requires TLS")
            if any(urlsplit(origin).scheme != "https" for origin in self.trusted_origins):
                raise ValueError("Production origins require HTTPS")
        return self
