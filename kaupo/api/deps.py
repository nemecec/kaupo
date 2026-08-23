"""Auth and shared dependencies.

Two bearer tokens: admin (full access) and read-only (GETs, used by agents).
When neither is configured, auth is disabled (local dev) with a startup warning.
"""

import logging
from typing import Annotated

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from kaupo.config import Settings, get_settings

log = logging.getLogger(__name__)

_bearer = HTTPBearer(auto_error=False)


class Principal:
    def __init__(self, admin: bool) -> None:
        self.admin = admin


def get_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(_bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Principal:
    if settings.auth_disabled:
        return Principal(admin=True)
    token = credentials.credentials if credentials else ""
    if settings.admin_token and token == settings.admin_token:
        return Principal(admin=True)
    if settings.readonly_token and token == settings.readonly_token:
        return Principal(admin=False)
        # avoid timing oracle on empty/unknown tokens: fall through to 401
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid or missing token",
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_admin(principal: Annotated[Principal, Depends(get_principal)]) -> Principal:
    if not principal.admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin token required")
    return principal
