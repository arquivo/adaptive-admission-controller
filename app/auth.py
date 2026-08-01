"""Keycloak JWT verification for `user_class`/`user_id` classification
(`docs/implementation_plan.md` §4.1, `requirements.md` FR-012).

This is purely a scoring/priority input — a verified token buys a request a
higher `RESEARCHER` base score (`config/backends.yaml` `scoring.base_scores`),
it never gates access. The AAC has no login flow of its own; it only verifies
tokens a front-end Keycloak realm already issued.

Role mapping (approved plan, `/home/ibranco/.claude/plans/soft-mixing-moonbeam.md`):
`realm_access.roles` containing `"internal"` -> `UserClass.INTERNAL`, else
`"service_account"` -> `UserClass.SERVICE_ACCOUNT`, else any other
successfully verified token -> `UserClass.RESEARCHER` (FR-012 defines that
class as "authenticated researcher"). No `Authorization` header at all ->
`UserClass.ANONYMOUS`. Header present but unverifiable (missing/unknown
`kid`, bad signature, expired, wrong issuer/audience) -> `UserClass.UNKNOWN`
(FR-012's own "verification failed or ambiguous" wording).

JWKS fetch failure (startup or periodic refresh) fails open: `verify()` then
always reports "no verifiable token" until a refresh succeeds — the same
"optional signal, don't crash on its absence" posture as `app.geoip`, and
does not gate `/readyz` (decision log A5's Redis-down precedent).
"""

from __future__ import annotations

import asyncio
import logging

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm

from app.config import AuthConfig
from app.interfaces import UserClass

logger = logging.getLogger(__name__)


class JWTVerifier:
    def __init__(self, config: AuthConfig) -> None:
        self._config = config
        self._keys: dict[str, object] = {}

    async def initial_fetch(self) -> None:
        if not self._config.enabled:
            return
        await self._safe_refresh()

    async def refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(self._config.jwks_refresh_seconds)
            await self._safe_refresh()

    async def _safe_refresh(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(str(self._config.jwks_url))
                response.raise_for_status()
                jwks = response.json()
            self._keys = {
                key["kid"]: RSAAlgorithm.from_jwk(key) for key in jwks["keys"] if "kid" in key
            }
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            logger.warning("jwks_refresh_failed", extra={"error": str(exc)})

    def verify(self, authorization_header: str | None) -> tuple[UserClass, str | None]:
        if not self._config.enabled or authorization_header is None:
            return UserClass.ANONYMOUS, None

        scheme, _, token = authorization_header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            return UserClass.UNKNOWN, None

        try:
            kid = jwt.get_unverified_header(token).get("kid")
            key = self._keys.get(kid)
            if key is None:
                return UserClass.UNKNOWN, None
            claims = jwt.decode(
                token,
                key=key,
                algorithms=["RS256"],
                issuer=self._config.issuer,
                audience=self._config.audience,
                options={
                    "require": ["exp", "iss"],
                    "verify_aud": self._config.audience is not None,
                },
            )
        except jwt.PyJWTError:
            return UserClass.UNKNOWN, None

        user_id = claims.get("sub")
        roles = claims.get("realm_access", {}).get("roles", [])
        if "internal" in roles:
            return UserClass.INTERNAL, user_id
        if "service_account" in roles:
            return UserClass.SERVICE_ACCOUNT, user_id
        return UserClass.RESEARCHER, user_id
