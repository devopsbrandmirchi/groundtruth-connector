"""
Claude.ai-compatible OAuth 2.1 provider for the Ground Truth MCP connector.

Claude custom connectors require Dynamic Client Registration (DCR) and
discovery at /.well-known/oauth-authorization-server.

Access tokens, refresh tokens, AND dynamically registered client ids are
**HMAC-signed (stateless)**. Nothing Claude holds depends on server memory, so
Cloud Run deploys, restarts, and cold starts never force a reconnect.

Refresh tokens are NOT rotated (same token returned on refresh).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

logger = logging.getLogger("ground-truth-oauth")

DEFAULT_SCOPE = "ground-truth"
DEFAULT_ACCESS_TOKEN_EXPIRY = 90 * 24 * 60 * 60  # 90 days
DEFAULT_REFRESH_TOKEN_EXPIRY = 10 * 365 * 24 * 60 * 60  # 10 years
SIGNED_CLIENT_PREFIX = "gtc_"
DEFAULT_ALLOWED_REDIRECT_DOMAINS = ("claude.ai", "claude.com", "localhost", "127.0.0.1")

CLAUDE_CIMD_URLS = (
    "https://claude.ai/oauth/mcp-oauth-client-metadata",
    "https://claude.com/oauth/mcp-oauth-client-metadata",
)

JWT_BEARER_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(raw: str) -> bytes:
    pad = "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(raw + pad)


def _resolve_signing_secret() -> str:
    """
    Stable secret across Cloud Run revisions (must not change or all sessions die).

    Prefer MCP_OAUTH_JWT_SECRET; fall back to GROUND_TRUTH_API_KEY (already in
    Secret Manager on Cloud Run).
    """
    for key in ("MCP_OAUTH_JWT_SECRET", "GROUND_TRUTH_API_KEY"):
        val = os.environ.get(key, "").strip()
        if val:
            return val
    # Local-only fallback — Cloud Run must have MCP_OAUTH_JWT_SECRET.
    return "ground-truth-mcp-dev-insecure-change-me"


class ClaudeOAuthProvider(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    """Embedded AS with DCR + PKCE + stateless signed tokens for Claude."""

    def __init__(
        self,
        *,
        base_url: str,
        password: Optional[str] = None,
        allowed_redirect_domains: Optional[list[str]] = None,
        access_token_expiry_seconds: int = DEFAULT_ACCESS_TOKEN_EXPIRY,
        scope: str = DEFAULT_SCOPE,
        state_path: Optional[str] = None,  # kept for call-site compat; unused
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.password = password
        self.allowed_redirect_domains = (
            list(allowed_redirect_domains)
            if allowed_redirect_domains is not None
            else list(DEFAULT_ALLOWED_REDIRECT_DOMAINS)
        )
        self.access_token_expiry_seconds = access_token_expiry_seconds
        self.refresh_token_expiry_seconds = DEFAULT_REFRESH_TOKEN_EXPIRY
        self.scope = scope
        self._signing_secret = _resolve_signing_secret()

        # Short-lived auth codes only (OK in memory — 5 minutes).
        self.auth_codes: dict[str, AuthorizationCode] = {}
        # Optional cache of DCR/CIMD clients (CIMD can always be re-fetched).
        self.clients: dict[str, OAuthClientInformationFull] = {}

        logger.info(
            "OAuth tokens are signed/stateless (survive Cloud Run deploys). "
            "secret_source=%s",
            "MCP_OAUTH_JWT_SECRET"
            if os.environ.get("MCP_OAUTH_JWT_SECRET", "").strip()
            else (
                "GROUND_TRUTH_API_KEY"
                if os.environ.get("GROUND_TRUTH_API_KEY", "").strip()
                else "insecure-dev-fallback"
            ),
        )

    # ------------------------------------------------------------------
    # Signed token helpers (stateless)
    # ------------------------------------------------------------------

    def _sign_payload(self, payload: dict[str, Any]) -> str:
        body = _b64url_encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        sig = _b64url_encode(
            hmac.new(
                self._signing_secret.encode("utf-8"),
                body.encode("ascii"),
                hashlib.sha256,
            ).digest()
        )
        return f"{body}.{sig}"

    def _verify_payload(self, token: str) -> Optional[dict[str, Any]]:
        try:
            body, sig = token.rsplit(".", 1)
        except ValueError:
            return None
        expected = _b64url_encode(
            hmac.new(
                self._signing_secret.encode("utf-8"),
                body.encode("ascii"),
                hashlib.sha256,
            ).digest()
        )
        if not hmac.compare_digest(sig, expected):
            return None
        try:
            data = json.loads(_b64url_decode(body).decode("utf-8"))
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        exp = data.get("exp")
        if exp is not None and int(exp) < int(time.time()):
            return None
        return data

    def _mint_access(
        self,
        *,
        client_id: str,
        scopes: list[str],
        subject: Optional[str],
        resource: Optional[str],
    ) -> tuple[str, int]:
        now = int(time.time())
        exp = now + self.access_token_expiry_seconds
        token = self._sign_payload(
            {
                "typ": "access",
                "cid": client_id,
                "scopes": scopes,
                "sub": subject or "ground-truth-user",
                "resource": resource,
                "iat": now,
                "exp": exp,
            }
        )
        return token, exp

    def _mint_refresh(
        self,
        *,
        client_id: str,
        scopes: list[str],
        subject: Optional[str],
    ) -> str:
        now = int(time.time())
        return self._sign_payload(
            {
                "typ": "refresh",
                "cid": client_id,
                "scopes": scopes,
                "sub": subject or "ground-truth-user",
                "iat": now,
                "exp": now + self.refresh_token_expiry_seconds,
            }
        )

    def _is_redirect_allowed(self, redirect_uri: str) -> bool:
        try:
            host = (urlparse(redirect_uri).hostname or "").lower()
        except Exception:
            return False
        if not host:
            return False
        for domain in self.allowed_redirect_domains:
            d = domain.lower()
            if host == d or host.endswith(f".{d}"):
                return True
        return False

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        existing = self.clients.get(client_id)
        if existing:
            return existing
        if client_id.startswith(SIGNED_CLIENT_PREFIX):
            return self._decode_signed_client(client_id)
        return await self._ensure_cimd_client(client_id)

    def _decode_signed_client(self, client_id: str) -> OAuthClientInformationFull | None:
        """Rebuild a DCR client from its signed client_id (survives restarts)."""
        data = self._verify_payload(client_id[len(SIGNED_CLIENT_PREFIX):])
        if not data or data.get("typ") != "client":
            return None
        redirect_uris = [u for u in (data.get("ru") or []) if self._is_redirect_allowed(u)]
        if not redirect_uris:
            return None
        client_info = OAuthClientInformationFull(
            client_id=client_id,
            client_name=data.get("n") or "Claude",
            redirect_uris=redirect_uris,
            grant_types=data.get("gt") or ["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
            scope=data.get("s") or self.scope,
        )
        self.clients[client_id] = client_info
        return client_info

    async def _ensure_cimd_client(self, client_id: str) -> OAuthClientInformationFull | None:
        """Accept Claude CIMD client_ids without a prior /register call."""
        if not client_id.startswith("https://"):
            return None

        host = (urlparse(client_id).hostname or "").lower()
        if not any(host == d or host.endswith(f".{d}") for d in self.allowed_redirect_domains):
            return None

        if client_id not in CLAUDE_CIMD_URLS and "/oauth/" not in client_id:
            return None

        try:
            res = httpx.get(client_id, timeout=10.0, follow_redirects=True)
            if not res.is_success:
                logger.warning("CIMD fetch failed for %s: %s", client_id, res.status_code)
                return None
            raw: dict[str, Any] = res.json()
        except Exception as exc:
            logger.warning("CIMD fetch error for %s: %s", client_id, exc)
            return None

        redirect_uris = [
            u for u in (raw.get("redirect_uris") or []) if self._is_redirect_allowed(u)
        ]
        if not redirect_uris:
            logger.warning("CIMD %s had no allowed redirect URIs", client_id)
            return None

        grant_types = [
            g
            for g in (raw.get("grant_types") or ["authorization_code", "refresh_token"])
            if g != JWT_BEARER_GRANT
        ]
        if "authorization_code" not in grant_types:
            grant_types.insert(0, "authorization_code")

        client_info = OAuthClientInformationFull(
            client_id=client_id,
            client_name=raw.get("client_name") or "Claude",
            redirect_uris=redirect_uris,
            grant_types=grant_types,
            response_types=raw.get("response_types") or ["code"],
            token_endpoint_auth_method="none",
            scope=self.scope,
        )
        self.clients[client_id] = client_info
        logger.info("Registered CIMD OAuth client %s", client_id)
        return client_info

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not client_info.client_id:
            raise ValueError("No client_id provided")
        # The SDK returns this same object to the client after we return, so
        # swapping in a signed id makes the registration self-describing: any
        # instance (including one started after a redeploy) can verify it.
        if (
            not client_info.client_id.startswith("https://")
            and client_info.token_endpoint_auth_method == "none"
        ):
            signed = self._sign_payload(
                {
                    "typ": "client",
                    "ru": [str(u) for u in (client_info.redirect_uris or [])],
                    "gt": list(client_info.grant_types or []),
                    "n": client_info.client_name or "Claude",
                    "s": client_info.scope or self.scope,
                }
            )
            client_info.client_id = f"{SIGNED_CLIENT_PREFIX}{signed}"
        self.clients[client_info.client_id] = client_info
        logger.info("Registered OAuth client %s", client_info.client_id)

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        redirect = str(params.redirect_uri)
        if not self._is_redirect_allowed(redirect):
            raise AuthorizeError(
                error="access_denied",
                error_description="Redirect URI domain not allowed for this connector.",
            )

        if self.password:
            provided = " ".join(params.scopes or [])
            if self.password not in provided and (params.state or "") != self.password:
                if not self._is_redirect_allowed(redirect):
                    raise AuthorizeError(
                        error="access_denied",
                        error_description="Authorization denied.",
                    )

        if not client.client_id:
            raise AuthorizeError(error="invalid_request", error_description="Missing client_id")

        scopes = list(params.scopes or [])
        if self.scope not in scopes:
            scopes.append(self.scope)

        code = f"mac_{secrets.token_hex(24)}"
        self.auth_codes[code] = AuthorizationCode(
            code=code,
            client_id=client.client_id,
            redirect_uri=AnyUrl(redirect),
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            expires_at=time.time() + 300,
            scopes=scopes,
            code_challenge=params.code_challenge,
            resource=params.resource,
            subject="ground-truth-user",
        )
        return construct_redirect_uri(redirect, code=code, state=params.state)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = self.auth_codes.get(authorization_code)
        if not code:
            return None
        if code.expires_at < time.time():
            del self.auth_codes[authorization_code]
            return None
        if code.client_id != client.client_id:
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        if authorization_code.code not in self.auth_codes:
            raise TokenError(error="invalid_grant", error_description="Unknown authorization code")
        if not client.client_id:
            raise TokenError(error="invalid_client", error_description="Missing client_id")

        del self.auth_codes[authorization_code.code]

        access, _exp = self._mint_access(
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            subject=authorization_code.subject,
            resource=str(authorization_code.resource) if authorization_code.resource else None,
        )
        refresh = self._mint_refresh(
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            subject=authorization_code.subject,
        )

        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=self.access_token_expiry_seconds,
            refresh_token=refresh,
            scope=" ".join(authorization_code.scopes),
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        data = self._verify_payload(token)
        if not data or data.get("typ") != "access":
            return None
        client_id = str(data.get("cid") or "")
        if not client_id:
            return None
        scopes = data.get("scopes") or [self.scope]
        if not isinstance(scopes, list):
            scopes = [self.scope]
        return AccessToken(
            token=token,
            client_id=client_id,
            scopes=[str(s) for s in scopes],
            expires_at=int(data["exp"]) if data.get("exp") is not None else None,
            resource=data.get("resource"),
            subject=str(data.get("sub") or "ground-truth-user"),
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        data = self._verify_payload(refresh_token)
        if not data or data.get("typ") != "refresh":
            return None
        if str(data.get("cid") or "") != client.client_id:
            return None
        scopes = data.get("scopes") or [self.scope]
        if not isinstance(scopes, list):
            scopes = [self.scope]
        return RefreshToken(
            token=refresh_token,
            client_id=client.client_id,
            scopes=[str(s) for s in scopes],
            expires_at=int(data["exp"]) if data.get("exp") is not None else None,
            subject=str(data.get("sub") or "ground-truth-user"),
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        # Re-verify signature (stateless — works after any Cloud Run restart).
        data = self._verify_payload(refresh_token.token)
        if not data or data.get("typ") != "refresh":
            raise TokenError(error="invalid_grant", error_description="Unknown refresh token")
        if not client.client_id or str(data.get("cid") or "") != client.client_id:
            raise TokenError(error="invalid_client", error_description="Missing client_id")

        granted = scopes or refresh_token.scopes or [self.scope]
        access, _exp = self._mint_access(
            client_id=client.client_id,
            scopes=granted,
            subject=refresh_token.subject,
            resource=None,
        )

        # Do NOT rotate refresh token — return the same signed refresh JWT.
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=self.access_token_expiry_seconds,
            refresh_token=refresh_token.token,
            scope=" ".join(granted),
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        # Stateless tokens cannot be revoked server-side without a denylist.
        # Claude disconnect is enough for this connector.
        return None
