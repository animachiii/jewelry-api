"""X-API-Key verification. See docs/api-routes.md — Auth scopes.

`client` scope is a subset of what `ops` can do (ops = client + internal/ops
routes), so `require_scope("client")` accepts either scope while
`require_scope("ops")` accepts only `ops`.
"""

from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, AuthError, ErrorCode
from app.db.models.api_clients import ApiClient
from app.db.repositories import api_clients as api_clients_repo
from app.db.session import get_db

_hasher = PasswordHasher()

_KEY_PREFIX_LENGTH = 8


class InsufficientScopeError(AppError):
    code = ErrorCode.INSUFFICIENT_SCOPE
    http_status = 403


async def get_current_client(
    session: Annotated[AsyncSession, Depends(get_db)],
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> ApiClient:
    if not x_api_key or len(x_api_key) < _KEY_PREFIX_LENGTH:
        raise AuthError("Missing or malformed API key.")

    client = await api_clients_repo.get_by_key_prefix(session, x_api_key[:_KEY_PREFIX_LENGTH])
    if client is None or not client.is_active:
        raise AuthError("Invalid API key.")

    try:
        _hasher.verify(client.key_hash, x_api_key)
    except VerifyMismatchError as exc:
        raise AuthError("Invalid API key.") from exc

    return client


def require_scope(
    scope: str,
) -> Callable[[ApiClient], Coroutine[Any, Any, ApiClient]]:
    async def _dependency(
        client: Annotated[ApiClient, Depends(get_current_client)],
    ) -> ApiClient:
        allowed = {"ops"} if scope == "ops" else {"client", "ops"}
        if client.scope not in allowed:
            raise InsufficientScopeError(
                f"This key does not have the '{scope}' scope.",
                details={"required_scope": scope, "client_scope": client.scope},
            )
        return client

    return _dependency


require_client_scope = require_scope("client")
require_ops_scope = require_scope("ops")
