"""SMTP delivery. Codes are sent to the mailbox, never returned or logged."""

import smtplib
import ssl
from email.message import EmailMessage
from typing import Protocol

from cinemind.config import Settings


class DeliveryFailed(Exception):
    pass


class Mailer(Protocol):
    def send_verification(self, email: str, code: str) -> None: ...


class SMTPMailer:
    def __init__(self, settings: Settings):
        self.settings = settings

    def send_verification(self, email: str, code: str) -> None:
        settings = self.settings
        message = EmailMessage()
        message["From"] = settings.smtp_from
        message["To"] = email
        message["Subject"] = "Verify your CineMind AI email"
        message.set_content(f"Your CineMind AI verification code is: {code}\n\n"
                            f"It expires in {settings.verification_ttl_seconds // 60} minutes.\n"
                            "If you did not request this, ignore this email.")
        try:
            if settings.smtp_tls == "ssl":
                connection = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port,
                                             timeout=settings.smtp_timeout_seconds, context=ssl.create_default_context())
            else:
                connection = smtplib.SMTP(settings.smtp_host, settings.smtp_port,
                                         timeout=settings.smtp_timeout_seconds)
            with connection as smtp:
                if settings.smtp_tls == "starttls":
                    smtp.starttls(context=ssl.create_default_context())
                if settings.smtp_username:
                    smtp.login(settings.smtp_username, settings.smtp_password.get_secret_value())
                smtp.send_message(message)
        except (OSError, smtplib.SMTPException):
            raise DeliveryFailed("Verification delivery unavailable") from None
