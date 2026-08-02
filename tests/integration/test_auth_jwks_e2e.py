"""Integration test for the one path `tests/unit/test_auth.py` deliberately
bypasses: the real JWKS HTTP fetch and JWK-to-key parsing in
`JWTVerifier._safe_refresh()`. Every unit test injects `_keys` directly and
never calls `initial_fetch()` against a live endpoint (see that file's own
module docstring) — this spins up a real HTTP server serving a genuine JWK
Set document, so the `httpx` GET, JSON parsing, and `RSAAlgorithm.from_jwk`
conversion all run for real, against a stand-in for Keycloak's `/certs`
endpoint rather than a mocked one.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager

import jwt
import pytest
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.auth import JWTVerifier
from app.config import AuthConfig
from app.interfaces import UserClass
from tests.integration.conftest import _free_port

pytestmark = pytest.mark.asyncio

_KID = "e2e-test-kid"
_ISSUER = "https://keycloak.example.org/realms/arquivo"


@asynccontextmanager
async def _running_jwks_server(jwk: dict):
    async def certs(_request):
        return JSONResponse({"keys": [{**jwk, "kid": _KID}]})

    app = Starlette(routes=[Route("/certs", certs)])
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:  # noqa: ASYNC110
            await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}/certs"
    finally:
        server.should_exit = True
        await task


def _make_token(private_key, *, roles=None, exp_delta=3600):
    claims = {
        "sub": "user-1",
        "iss": _ISSUER,
        "exp": int(time.time()) + exp_delta,
        "realm_access": {"roles": roles or []},
    }
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": _KID})


async def test_initial_fetch_verifies_token_via_real_jwks_http_round_trip():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)

    async with _running_jwks_server(jwk) as jwks_url:
        config = AuthConfig(enabled=True, issuer=_ISSUER, jwks_url=jwks_url, audience=None)
        verifier = JWTVerifier(config)
        await verifier.initial_fetch()  # real httpx GET + JWK parsing, not injected _keys

        token = _make_token(private_key, roles=["internal"])
        user_class, user_id = verifier.verify(f"Bearer {token}")

    assert user_class == UserClass.INTERNAL
    assert user_id == "user-1"


async def test_token_signed_by_unregistered_key_is_unknown_via_real_jwks():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)

    async with _running_jwks_server(jwk) as jwks_url:
        config = AuthConfig(enabled=True, issuer=_ISSUER, jwks_url=jwks_url, audience=None)
        verifier = JWTVerifier(config)
        await verifier.initial_fetch()

        token = _make_token(other_key)
        user_class, user_id = verifier.verify(f"Bearer {token}")

    assert user_class == UserClass.UNKNOWN
    assert user_id is None
