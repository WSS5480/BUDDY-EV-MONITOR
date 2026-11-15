"""OAuth for the Claude connector, plus the owner's web sign-in.

Claude adds this app as a custom connector. When it connects, it registers itself (dynamic
client registration) and sends Steve to /authorize. We park that request and show the owner
password page; only the right OWNER_PASSWORD issues an authorization code. Tokens are stored
hashed, so a database leak doesn't hand out working tokens.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import time
from typing import Any
from urllib.parse import urlparse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from . import config
from .store import store

K_CLIENT = "oauth:client:"
K_PENDING = "oauth:pending:"
K_CODE = "oauth:code:"
K_ACCESS = "oauth:access:"
K_REFRESH = "oauth:refresh:"
K_SESSION = "web:session:"
K_FAILS = "web:fails:"  # + client IP

# Only Claude's own connector callbacks may receive codes (plus localhost when running locally),
# so a phishing link registered by someone else can't collect a token.
ALLOWED_REDIRECTS = {
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
}
MAX_CLIENTS = 25
CLIENT_TTL = 60 * 60 * 24 * 180
_fail_lock = asyncio.Lock()


def redirect_allowed(uri: str) -> bool:
    if uri in ALLOWED_REDIRECTS:
        return True
    if config.APP_URL.startswith(("http://localhost", "http://127.0.0.1")):
        return urlparse(uri).hostname in ("localhost", "127.0.0.1")
    return False

SESSION_COOKIE = "bem_session"
SESSION_TTL = 60 * 60 * 24 * 30


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---------- owner password + web sessions ----------


async def check_password(password: str, ip: str = "?") -> bool:
    """Constant-time check; 5 wrong tries from one address locks that address out for 15 minutes."""
    if not password_ok_length():
        return False  # a short OWNER_PASSWORD is refused outright (guessable online)
    key = K_FAILS + ip
    async with _fail_lock:  # one Render instance, so an in-process lock makes the counter atomic
        fails = await store.get(key) or {"n": 0, "until": 0}
        if fails["until"] > time.time():
            return False
        ok = hmac.compare_digest(password.encode(), config.OWNER_PASSWORD.encode())
        if ok:
            await store.delete(key)
            return True
        n = fails["n"] + 1
        until = time.time() + 15 * 60 if n >= 5 else 0
        await store.set(key, {"n": 0 if until else n, "until": until}, ttl=60 * 60)
    await asyncio.sleep(1)  # slow each wrong try, without making the owner wait behind others
    return False


def password_ok_length() -> bool:
    return len(config.OWNER_PASSWORD) >= config.MIN_PASSWORD_LEN


async def locked_out(ip: str = "?") -> bool:
    fails = await store.get(K_FAILS + ip) or {"until": 0}
    return fails["until"] > time.time()


async def new_session() -> str:
    token = secrets.token_urlsafe(32)
    await store.set(K_SESSION + _h(token), {"at": time.time()}, ttl=SESSION_TTL)
    return token


async def valid_session(token: str | None) -> bool:
    if not token:
        return False
    return await store.get(K_SESSION + _h(token)) is not None


async def end_session(token: str | None) -> None:
    if token:
        await store.delete(K_SESSION + _h(token))


# ---------- OAuth provider for the MCP connector ----------


class OwnerOAuthProvider:
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        data = await store.get(K_CLIENT + client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = [str(u) for u in (client_info.redirect_uris or [])]
        if not uris or not all(redirect_allowed(u) for u in uris):
            raise RegistrationError("invalid_redirect_uri", "Only Claude's connector callback is allowed.")
        if await store.count_prefix(K_CLIENT) >= MAX_CLIENTS:
            raise RegistrationError(
                "invalid_client_metadata", "Too many connections; disconnect Claude on the app page first."
            )
        await store.set(K_CLIENT + client_info.client_id, client_info.model_dump(mode="json"), ttl=CLIENT_TTL)

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        req_id = secrets.token_urlsafe(24)
        await store.set(
            K_PENDING + req_id,
            {
                "client_id": client.client_id,
                "state": params.state,
                "scopes": params.scopes or [config.CONNECTOR_SCOPE],
                "code_challenge": params.code_challenge,
                "redirect_uri": str(params.redirect_uri),
                "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
                "resource": params.resource,
                "client_name": client.client_name,
                "redirect_host": urlparse(str(params.redirect_uri)).hostname,
            },
            ttl=10 * 60,
        )
        return f"{config.APP_URL}/connect?req={req_id}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        data = await store.get(K_CODE + _h(authorization_code))
        if not data or data["client_id"] != client.client_id:
            return None
        return AuthorizationCode.model_validate({**data, "code": authorization_code})

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        if await store.take(K_CODE + _h(authorization_code.code)) is None:
            raise TokenError("invalid_grant", "authorization code already used")
        return await self._issue(client.client_id, authorization_code.scopes, authorization_code.resource)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        data = await store.get(K_REFRESH + _h(refresh_token))
        if not data or data["client_id"] != client.client_id:
            return None
        return RefreshToken.model_validate({**data, "token": refresh_token})

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        if await store.take(K_REFRESH + _h(refresh_token.token)) is None:
            raise TokenError("invalid_grant", "refresh token already used")
        return await self._issue(client.client_id, scopes or refresh_token.scopes, refresh_token.resource)

    async def load_access_token(self, token: str) -> AccessToken | None:
        data = await store.get(K_ACCESS + _h(token))
        if not data:
            return None
        return AccessToken.model_validate({**data, "token": token})

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        await store.delete(K_ACCESS + _h(token.token))
        await store.delete(K_REFRESH + _h(token.token))

    async def _issue(self, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        now = int(time.time())
        await store.set(
            K_ACCESS + _h(access),
            {
                "client_id": client_id,
                "scopes": scopes,
                "expires_at": now + config.CONNECTOR_ACCESS_TTL,
                "resource": resource,
                "subject": "owner",
            },
            ttl=config.CONNECTOR_ACCESS_TTL,
        )
        await store.set(
            K_REFRESH + _h(refresh),
            {
                "client_id": client_id,
                "scopes": scopes,
                "expires_at": now + config.CONNECTOR_REFRESH_TTL,
                "resource": resource,
                "subject": "owner",
            },
            ttl=config.CONNECTOR_REFRESH_TTL,
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=config.CONNECTOR_ACCESS_TTL,
            refresh_token=refresh,
            scope=" ".join(scopes),
        )


async def pending_request(req_id: str) -> dict[str, Any] | None:
    return await store.get(K_PENDING + req_id)


async def approve_request(req_id: str) -> str:
    """Owner entered the right password: issue a code and return the redirect back to Claude."""
    data = await store.take(K_PENDING + req_id)
    if not data:
        raise AuthorizeError("invalid_request", "This sign-in request expired. Start again from Claude.")
    code = secrets.token_urlsafe(32)
    await store.set(
        K_CODE + _h(code),
        {
            "client_id": data["client_id"],
            "scopes": data["scopes"],
            "expires_at": time.time() + 5 * 60,
            "code_challenge": data["code_challenge"],
            "redirect_uri": data["redirect_uri"],
            "redirect_uri_provided_explicitly": data["redirect_uri_provided_explicitly"],
            "resource": data.get("resource"),
            "subject": "owner",
        },
        ttl=5 * 60,
    )
    return construct_redirect_uri(data["redirect_uri"], code=code, state=data.get("state"))


async def disconnect_all_connectors() -> None:
    """Cut off every Claude connection (clients, tokens, pending sign-ins)."""
    for prefix in (K_CLIENT, K_PENDING, K_CODE, K_ACCESS, K_REFRESH):
        await store.delete_prefix(prefix)


async def sign_out_everywhere() -> None:
    await store.delete_prefix(K_SESSION)
