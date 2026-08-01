"""Unit tests for app.classifier — real `classify()` wiring
(`docs/implementation_plan.md` §4.1). `geoip`/`auth` are simple fakes, so
these tests exercise only the classifier's own logic: subnet_24/subnet_6
computation and forwarding to the injected collaborators.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.classifier import classify
from app.interfaces import UserClass


class _FakeGeoIP:
    def __init__(self, result=(None, None)):
        self.result = result
        self.calls = []

    def lookup(self, ip):
        self.calls.append(ip)
        return self.result


class _FakeAuth:
    def __init__(self, result=(UserClass.ANONYMOUS, None)):
        self.result = result
        self.calls = []

    def verify(self, header):
        self.calls.append(header)
        return self.result


def _request(*, client_ip="1.2.3.4", authorization=None, path="/test", method="GET"):
    return SimpleNamespace(
        state=SimpleNamespace(client_ip=client_ip),
        url=SimpleNamespace(path=path),
        method=method,
        headers={"authorization": authorization} if authorization else {},
    )


def _policy(*, name="test-backend", ipv6_prefix_length=56):
    return SimpleNamespace(
        config=SimpleNamespace(name=name),
        resolved_scoring=SimpleNamespace(ipv6_prefix_length=ipv6_prefix_length),
    )


def test_ipv4_source_computes_subnet_24_not_subnet_6():
    ctx = classify(_request(client_ip="203.0.113.42"), _policy(), _FakeGeoIP(), _FakeAuth())

    assert ctx.subnet_24 == "203.0.113.0/24"
    assert ctx.subnet_6 is None


def test_ipv6_source_computes_subnet_6_not_subnet_24():
    ctx = classify(
        _request(client_ip="2001:db8:abcd::1"),
        _policy(ipv6_prefix_length=48),
        _FakeGeoIP(),
        _FakeAuth(),
    )

    assert ctx.subnet_24 is None
    assert ctx.subnet_6 == "2001:db8:abcd::/48"


def test_missing_source_ip_skips_geoip_lookup():
    geoip = _FakeGeoIP()
    ctx = classify(_request(client_ip=None), _policy(), geoip, _FakeAuth())

    assert geoip.calls == []
    assert ctx.country is None
    assert ctx.asn is None
    assert ctx.subnet_24 is None
    assert ctx.subnet_6 is None


def test_invalid_source_ip_yields_no_subnets():
    ctx = classify(_request(client_ip="not-an-ip"), _policy(), _FakeGeoIP(), _FakeAuth())

    assert ctx.subnet_24 is None
    assert ctx.subnet_6 is None


def test_country_and_asn_come_from_geoip_lookup():
    geoip = _FakeGeoIP(result=("PT", "1930"))
    ctx = classify(_request(client_ip="1.2.3.4"), _policy(), geoip, _FakeAuth())

    assert geoip.calls == ["1.2.3.4"]
    assert ctx.country == "PT"
    assert ctx.asn == "1930"


def test_user_class_and_user_id_come_from_auth_verify():
    auth = _FakeAuth(result=(UserClass.RESEARCHER, "user-42"))
    ctx = classify(
        _request(authorization="Bearer sometoken"), _policy(), _FakeGeoIP(), auth
    )

    assert auth.calls == ["Bearer sometoken"]
    assert ctx.user_class == "researcher"
    assert ctx.user_id == "user-42"


def test_no_authorization_header_forwards_none_to_auth():
    auth = _FakeAuth()
    classify(_request(authorization=None), _policy(), _FakeGeoIP(), auth)

    assert auth.calls == [None]


def test_backend_path_and_method_copied_from_request():
    ctx = classify(
        _request(path="/textsearch/foo", method="POST"), _policy(name="page-search-api"),
        _FakeGeoIP(), _FakeAuth(),
    )

    assert ctx.backend == "page-search-api"
    assert ctx.path == "/textsearch/foo"
    assert ctx.method == "POST"
