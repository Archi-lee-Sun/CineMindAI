from typing import Annotated

from fastapi import Depends, Request

from cinemind.security import CurrentUser
from cinemind.services.auth import AuthService


def auth(request: Request) -> AuthService:
    return request.app.state.auth


def current_user(request: Request, service: Annotated[AuthService, Depends(auth)]) -> CurrentUser:
    return service.current(request.cookies.get(request.app.state.settings.session_cookie_name))


Identity = Annotated[CurrentUser, Depends(current_user)]
Auth = Annotated[AuthService, Depends(auth)]


def client_signal(request: Request) -> str:
    # Do not trust caller-supplied X-Forwarded-For. Configure trusted proxies at deployment.
    return request.client.host if request.client else "unknown"
