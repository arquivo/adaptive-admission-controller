"""Unit tests for app.ingress — trusted-proxy IP resolution (FR-010a)."""

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.ingress import TrustedProxyMiddleware, _ip_in_allowlist, resolve_client_ip

# --- Pure-function tests: no app needed -----------------------------------


@pytest.mark.parametrize(
    "xff,fallback,hops,expected",
    [
        ("1.2.3.4", "127.0.0.1", 1, "1.2.3.4"),
        ("1.2.3.4, 5.6.7.8", "127.0.0.1", 1, "5.6.7.8"),
        ("1.2.3.4, 5.6.7.8", "127.0.0.1", 2, "1.2.3.4"),
        (None, "127.0.0.1", 1, "127.0.0.1"),
        ("", "127.0.0.1", 1, "127.0.0.1"),
        ("1.2.3.4", "127.0.0.1", 3, "127.0.0.1"),  # fewer entries than hops -> fallback
        ("2001:db8::1, 5.6.7.8", "127.0.0.1", 2, "2001:db8::1"),
    ],
)
def test_resolve_client_ip(xff, fallback, hops, expected):
    assert resolve_client_ip(xff, fallback, hops) == expected


@pytest.mark.parametrize(
    "peer_ip,proxies,expected",
    [
        ("127.0.0.1", ["127.0.0.1", "::1"], True),
        ("::1", ["127.0.0.1", "::1"], True),
        ("10.0.0.5", ["127.0.0.1", "::1"], False),
        ("10.0.0.5", ["10.0.0.0/24"], True),
        ("10.0.1.5", ["10.0.0.0/24"], False),
        ("not-an-ip", ["127.0.0.1"], False),
    ],
)
def test_ip_in_allowlist(peer_ip, proxies, expected):
    assert _ip_in_allowlist(peer_ip, proxies) is expected


# --- Full middleware tests: exercised through a minimal Starlette app -----


class _FakeConfig:
    def __init__(self, trusted_proxies, xff_trusted_hops=1):
        self.ingress = _FakeIngress(trusted_proxies, xff_trusted_hops)


class _FakeIngress:
    def __init__(self, trusted_proxies, xff_trusted_hops):
        self.trusted_proxies = trusted_proxies
        self.xff_trusted_hops = xff_trusted_hops


def _build_app(trusted_proxies, xff_trusted_hops=1):
    async def echo_client_ip(request):
        return JSONResponse({"client_ip": request.state.client_ip})

    app = Starlette(routes=[Route("/", echo_client_ip)])
    app.add_middleware(TrustedProxyMiddleware)
    app.state.config = _FakeConfig(trusted_proxies, xff_trusted_hops)
    return app


def test_allowlisted_peer_with_valid_xff_resolves_correct_client_ip():
    app = _build_app(["127.0.0.1"])
    client = TestClient(app, client=("127.0.0.1", 12345))
    resp = client.get("/", headers={"x-forwarded-for": "9.9.9.9"})
    assert resp.status_code == 200
    assert resp.json()["client_ip"] == "9.9.9.9"


def test_allowlisted_peer_with_multi_hop_xff():
    app = _build_app(["127.0.0.1"], xff_trusted_hops=2)
    client = TestClient(app, client=("127.0.0.1", 12345))
    resp = client.get("/", headers={"x-forwarded-for": "1.2.3.4, 9.9.9.9"})
    assert resp.status_code == 200
    assert resp.json()["client_ip"] == "1.2.3.4"


def test_non_allowlisted_peer_rejected_403_regardless_of_xff():
    app = _build_app(["10.0.0.1"])  # does not include TestClient's peer
    client = TestClient(app, client=("127.0.0.1", 12345))
    resp = client.get("/", headers={"x-forwarded-for": "9.9.9.9"})
    assert resp.status_code == 403


def test_non_allowlisted_peer_rejected_403_even_without_xff():
    app = _build_app(["10.0.0.1"])
    client = TestClient(app, client=("127.0.0.1", 12345))
    resp = client.get("/")
    assert resp.status_code == 403


def test_allowlisted_ipv6_peer():
    app = _build_app(["::1"])
    client = TestClient(app, client=("::1", 12345))
    resp = client.get("/", headers={"x-forwarded-for": "2001:db8::1"})
    assert resp.status_code == 200
    assert resp.json()["client_ip"] == "2001:db8::1"
