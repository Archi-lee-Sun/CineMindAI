from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Query, Request, Response

from cinemind.schemas import Acknowledged, EmailRequest, Login, Profile, SessionView, Signup, VerifyEmail
from cinemind.services.auth import GENERIC_DELIVERY
from cinemind.routes.dependencies import Auth, Identity, client_signal

router = APIRouter(prefix="/auth", tags=["authentication"])


def clear_cookie(request: Request, response: Response) -> None:
    settings = request.app.state.settings
    response.delete_cookie(settings.session_cookie_name, path="/", secure=settings.session_cookie_secure,
                           httponly=True, samesite=settings.session_cookie_samesite)


@router.post("/signup", response_model=Acknowledged, status_code=202)
def signup(data: Signup, request: Request, background: BackgroundTasks, service: Auth) -> Acknowledged:
    service.signup(data, client_signal(request), defer=background.add_task)
    return Acknowledged(message=GENERIC_DELIVERY)


@router.post("/verify-email", response_model=Acknowledged)
def verify_email(data: VerifyEmail, request: Request, service: Auth) -> Acknowledged:
    service.verify(data, client_signal(request))
    return Acknowledged(message="Email verified. Log in separately to continue.")


@router.post("/resend-verification", response_model=Acknowledged, status_code=202)
def resend(data: EmailRequest, request: Request, background: BackgroundTasks, service: Auth) -> Acknowledged:
    service.resend(data.email, client_signal(request), defer=background.add_task)
    return Acknowledged(message=GENERIC_DELIVERY)


@router.post("/login", response_model=Profile)
def login(data: Login, request: Request, response: Response, service: Auth) -> Profile:
    profile, token = service.login(data, client_signal(request))
    settings = request.app.state.settings
    response.set_cookie(settings.session_cookie_name, token, max_age=settings.session_ttl_seconds,
                        path="/", secure=settings.session_cookie_secure, httponly=True,
                        samesite=settings.session_cookie_samesite)
    return profile


@router.post("/logout", response_model=Acknowledged)
def logout(request: Request, response: Response, service: Auth) -> Acknowledged:
    service.logout(request.cookies.get(request.app.state.settings.session_cookie_name))
    clear_cookie(request, response)
    return Acknowledged(message="Logged out.")


@router.get("/me", response_model=Profile)
def me(identity: Identity, service: Auth) -> Profile:
    return service.profile(identity)


@router.get("/sessions", response_model=list[SessionView])
def sessions(identity: Identity, service: Auth, limit: int = Query(50, ge=1, le=100),
             offset: int = Query(0, ge=0, le=100000)) -> list[SessionView]:
    return service.sessions(identity, limit, offset)


@router.delete("/sessions/{session_id}", status_code=204)
def revoke(session_id: UUID, request: Request, response: Response, identity: Identity, service: Auth) -> None:
    if service.revoke_session(identity, session_id):
        clear_cookie(request, response)
