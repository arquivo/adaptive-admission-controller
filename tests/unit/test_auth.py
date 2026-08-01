"""Unit tests for app.auth — Keycloak JWT verification (`docs/implementation_plan.md`
§4.1, `docs/decision_log.md` A4/A5). Uses a locally-signed RSA keypair; no
live Keycloak/JWKS endpoint needed — `JWTVerifier._keys` is populated
directly, bypassing the `httpx` fetch (`verify()` is the only method under
test here).
"""

from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.auth import JWTVerifier
from app.config import AuthConfig
from app.interfaces import UserClass

_KID = "test-kid"
_ISSUER = "https://keycloak.example.org/realms/arquivo"


@pytest.fixture(scope="module")
def keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


@pytest.fixture
def verifier(keypair):
    _private_key, public_key = keypair
    config = AuthConfig(
        enabled=True,
        issuer=_ISSUER,
        jwks_url="https://keycloak.example.org/realms/arquivo/protocol/openid-connect/certs",
        audience=None,
    )
    v = JWTVerifier(config)
    v._keys = {_KID: public_key}
    return v


def _make_token(private_key, *, roles=None, issuer=_ISSUER, exp_delta=3600, sub="user-1"):
    claims = {
        "sub": sub,
        "iss": issuer,
        "exp": int(time.time()) + exp_delta,
        "realm_access": {"roles": roles or []},
    }
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": _KID})


def test_no_header_is_anonymous(verifier):
    user_class, user_id = verifier.verify(None)
    assert user_class == UserClass.ANONYMOUS
    assert user_id is None


def test_disabled_config_is_always_anonymous(keypair):
    _private_key, public_key = keypair
    config = AuthConfig(enabled=False)
    v = JWTVerifier(config)
    v._keys = {_KID: public_key}

    token = _make_token(keypair[0])
    user_class, user_id = v.verify(f"Bearer {token}")

    assert user_class == UserClass.ANONYMOUS
    assert user_id is None


def test_valid_token_with_no_roles_is_researcher(verifier, keypair):
    token = _make_token(keypair[0])
    user_class, user_id = verifier.verify(f"Bearer {token}")

    assert user_class == UserClass.RESEARCHER
    assert user_id == "user-1"


def test_valid_token_with_internal_role(verifier, keypair):
    token = _make_token(keypair[0], roles=["internal"])
    user_class, _ = verifier.verify(f"Bearer {token}")
    assert user_class == UserClass.INTERNAL


def test_valid_token_with_service_account_role(verifier, keypair):
    token = _make_token(keypair[0], roles=["service_account"])
    user_class, _ = verifier.verify(f"Bearer {token}")
    assert user_class == UserClass.SERVICE_ACCOUNT


def test_internal_role_wins_over_service_account(verifier, keypair):
    token = _make_token(keypair[0], roles=["service_account", "internal"])
    user_class, _ = verifier.verify(f"Bearer {token}")
    assert user_class == UserClass.INTERNAL


def test_expired_token_is_unknown(verifier, keypair):
    token = _make_token(keypair[0], exp_delta=-3600)
    user_class, user_id = verifier.verify(f"Bearer {token}")
    assert user_class == UserClass.UNKNOWN
    assert user_id is None


def test_wrong_issuer_is_unknown(verifier, keypair):
    token = _make_token(keypair[0], issuer="https://not-the-right-realm.example.org")
    user_class, _ = verifier.verify(f"Bearer {token}")
    assert user_class == UserClass.UNKNOWN


def test_bad_signature_is_unknown(verifier):
    other_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = _make_token(other_private_key)
    user_class, _ = verifier.verify(f"Bearer {token}")
    assert user_class == UserClass.UNKNOWN


def test_unknown_kid_is_unknown(verifier, keypair):
    private_key, _public_key = keypair
    claims = {
        "sub": "user-1",
        "iss": _ISSUER,
        "exp": int(time.time()) + 3600,
        "realm_access": {"roles": []},
    }
    token = jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "unregistered-kid"})
    user_class, _ = verifier.verify(f"Bearer {token}")
    assert user_class == UserClass.UNKNOWN


def test_non_bearer_scheme_is_unknown(verifier, keypair):
    token = _make_token(keypair[0])
    user_class, _ = verifier.verify(f"Basic {token}")
    assert user_class == UserClass.UNKNOWN


async def test_jwks_refresh_failure_fails_open(keypair):
    """Keycloak unreachable at startup/refresh: `initial_fetch()`/
    `refresh_loop()` must never raise — verification degrades to `UNKNOWN`
    (no usable keys) rather than crashing the process (decision log A5's
    Redis-down precedent, applied to Keycloak)."""
    config = AuthConfig(
        enabled=True,
        issuer=_ISSUER,
        jwks_url="https://unreachable.invalid/certs",
        audience=None,
    )
    v = JWTVerifier(config)

    await v.initial_fetch()  # must not raise despite the unreachable URL

    token = _make_token(keypair[0])
    user_class, _ = v.verify(f"Bearer {token}")
    assert user_class == UserClass.UNKNOWN
