"""FastAPI composition, liveness/readiness and a strict cookie-write boundary."""

from contextlib import asynccontextmanager
from ipaddress import ip_address

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException
from starlette.concurrency import run_in_threadpool

from cinemind.config import Settings
from cinemind.database import Clock, Database, utcnow
from cinemind.errors import ServiceError
from cinemind.mail import Mailer, SMTPMailer
from cinemind.routes import auth, persistence
from cinemind.schemas import ErrorResponse
from cinemind.services.auth import AuthService
from cinemind.services.conversations import Conversations
from cinemind.services.documents import Documents
from cinemind.services.memories import Memories


def error_response(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}},
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


def loopback(host: str | None) -> bool:
    if host == "localhost":
        return True
    try:
        return ip_address(host or "").is_loopback
    except ValueError:
        return False


def create_app(settings: Settings | None = None, *, db: Database | None = None,
               mailer: Mailer | None = None, clock: Clock = utcnow) -> FastAPI:
    settings = settings or Settings()
    database = db or Database(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        if db is None:
            await run_in_threadpool(database.close)

    app = FastAPI(title="CineMind AI", lifespan=lifespan, responses={
        status: {"model": ErrorResponse} for status in (400, 401, 403, 404, 409, 422, 429, 503)
    })
    app.state.settings = settings
    app.state.database = database
    app.state.auth = AuthService(database, settings, mailer or SMTPMailer(settings), clock)
    app.state.conversations = Conversations(database, clock)
    app.state.memories = Memories(database, clock)
    app.state.documents = Documents(database, clock)

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, error: ServiceError):
        response = error_response(error.status, error.code, error.message)
        if error.status == 429:
            response.headers["Retry-After"] = str(settings.auth_rate_window_seconds)
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        # Default validation details may echo passwords and verification codes.
        return error_response(422, "validation_error", "Request validation failed.")

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException):
        return error_response(error.status_code, "http_error", "Request could not be completed.")

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, error: SQLAlchemyError):
        return error_response(503, "database_unavailable", "Database unavailable.")

    @app.middleware("http")
    async def secure_boundary(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if (request.headers.get("origin") not in settings.trusted_origins
                    or request.headers.get("x-cinemind-csrf") != "1"):
                return error_response(403, "csrf_rejected", "A trusted Origin and X-CineMind-CSRF header are required.")
            if settings.environment != "test" and not settings.session_cookie_secure:
                if not loopback(request.url.hostname) or not (request.client and loopback(request.client.host)):
                    return error_response(403, "https_required", "Use HTTPS with Secure cookies outside loopback development.")
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Liveness only; never claims database or AI readiness."""
        return {"status": "ok"}

    @app.get("/ready")
    def ready():
        try:
            database.require()
            with database.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except (SQLAlchemyError, ServiceError):
            return error_response(503, "database_unavailable", "Database unavailable.")
        return {"status": "ready"}

    app.include_router(auth.router)
    app.include_router(persistence.router)
    app.add_middleware(CORSMiddleware, allow_origins=settings.trusted_origins,
                       allow_credentials=True, allow_methods=["GET", "POST", "PATCH", "DELETE"],
                       allow_headers=["Content-Type", "X-CineMind-CSRF"])
    return app


app = create_app()
