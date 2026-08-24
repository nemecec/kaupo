"""Auth and shared dependencies.

Two bearer tokens: admin (full access) and read-only (GETs, used by agents).
When neither is configured, auth is disabled (local dev) with a startup warning.
"""

import hmac
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


def check_token(token: str, settings: Settings) -> Principal | None:
    """Validate a token against the configured ones (constant-time).

    Returns a Principal or None. When auth is disabled, returns admin.
    """
    if settings.auth_disabled:
        return Principal(admin=True)
    # encode both sides: compare_digest raises TypeError on non-ASCII str
    token_b = token.encode("utf-8", "replace")
    if settings.admin_token and hmac.compare_digest(token_b, settings.admin_token.encode()):
        return Principal(admin=True)
    if settings.readonly_token and hmac.compare_digest(token_b, settings.readonly_token.encode()):
        return Principal(admin=False)
    return None


def get_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(_bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Principal:
    token = credentials.credentials if credentials else ""
    principal = check_token(token, settings)
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return principal


def require_admin(principal: Annotated[Principal, Depends(get_principal)]) -> Principal:
    if not principal.admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin token required")
    return principal
