"""Unit tests for app.geoip — GeoIP/ASN lookup (FR-013/FR-013a).

No real MaxMind `.mmdb` file is used: the missing-file fail-open path is
exercised for real (`maxminddb.open_database` genuinely raises
`FileNotFoundError`), while the lookup/cache paths substitute a fake reader
via monkeypatching `app.geoip.maxminddb.open_database` — this project ships
no MaxMind database writer to build a real fixture file with.
"""

from __future__ import annotations

import maxminddb

import app.geoip as geoip_module
from app.geoip import GeoIPLookup


class _FakeReader:
    def __init__(self, records: dict[str, dict]):
        self._records = records
        self.get_calls = 0
        self.closed = False

    def get(self, ip):
        self.get_calls += 1
        return self._records.get(ip)

    def close(self):
        self.closed = True


def test_missing_db_file_fails_open(tmp_path, caplog):
    missing_path = tmp_path / "does-not-exist.mmdb"

    with caplog.at_level("WARNING"):
        lookup = GeoIPLookup(missing_path)

    assert lookup.lookup("1.2.3.4") == (None, None)
    assert "geoip_db_unavailable" in caplog.text


def test_invalid_db_file_fails_open(monkeypatch, tmp_path):
    def _raise(*_a, **_k):
        raise maxminddb.InvalidDatabaseError("corrupt")

    monkeypatch.setattr(geoip_module.maxminddb, "open_database", _raise)

    lookup = GeoIPLookup(tmp_path / "corrupt.mmdb")

    assert lookup.lookup("1.2.3.4") == (None, None)


def test_lookup_extracts_country_and_asn(monkeypatch, tmp_path):
    fake_reader = _FakeReader(
        {"1.2.3.4": {"country": {"iso_code": "PT"}, "autonomous_system_number": 64500}}
    )
    monkeypatch.setattr(geoip_module.maxminddb, "open_database", lambda *_a, **_k: fake_reader)

    lookup = GeoIPLookup(tmp_path / "db.mmdb")

    assert lookup.lookup("1.2.3.4") == ("PT", "64500")


def test_lookup_missing_fields_return_none(monkeypatch, tmp_path):
    """An ASN-only database has no `country` key; a City-only database has no
    `autonomous_system_number` — either/both may legitimately be absent."""
    fake_reader = _FakeReader({"1.2.3.4": {"autonomous_system_number": 64500}})
    monkeypatch.setattr(geoip_module.maxminddb, "open_database", lambda *_a, **_k: fake_reader)

    lookup = GeoIPLookup(tmp_path / "db.mmdb")

    assert lookup.lookup("1.2.3.4") == (None, "64500")


def test_lookup_unknown_ip_returns_none(monkeypatch, tmp_path):
    fake_reader = _FakeReader({})
    monkeypatch.setattr(geoip_module.maxminddb, "open_database", lambda *_a, **_k: fake_reader)

    lookup = GeoIPLookup(tmp_path / "db.mmdb")

    assert lookup.lookup("9.9.9.9") == (None, None)


def test_repeated_lookup_hits_cache_not_reader(monkeypatch, tmp_path):
    fake_reader = _FakeReader({"1.2.3.4": {"country": {"iso_code": "PT"}}})
    monkeypatch.setattr(geoip_module.maxminddb, "open_database", lambda *_a, **_k: fake_reader)

    lookup = GeoIPLookup(tmp_path / "db.mmdb")
    lookup.lookup("1.2.3.4")
    lookup.lookup("1.2.3.4")
    lookup.lookup("1.2.3.4")

    assert fake_reader.get_calls == 1


def test_expired_cache_entry_re_queries_reader(monkeypatch, tmp_path):
    fake_reader = _FakeReader({"1.2.3.4": {"country": {"iso_code": "PT"}}})
    monkeypatch.setattr(geoip_module.maxminddb, "open_database", lambda *_a, **_k: fake_reader)
    monkeypatch.setattr(geoip_module, "_CACHE_TTL_SECONDS", 0)

    lookup = GeoIPLookup(tmp_path / "db.mmdb")
    lookup.lookup("1.2.3.4")
    lookup.lookup("1.2.3.4")

    assert fake_reader.get_calls == 2


def test_cache_evicts_oldest_entry_at_size_cap(monkeypatch, tmp_path):
    fake_reader = _FakeReader({})
    monkeypatch.setattr(geoip_module.maxminddb, "open_database", lambda *_a, **_k: fake_reader)
    monkeypatch.setattr(geoip_module, "_CACHE_MAX_SIZE", 2)

    lookup = GeoIPLookup(tmp_path / "db.mmdb")
    lookup.lookup("1.1.1.1")
    lookup.lookup("2.2.2.2")
    lookup.lookup("3.3.3.3")  # evicts 1.1.1.1, the oldest

    assert "1.1.1.1" not in lookup._cache
    assert "2.2.2.2" in lookup._cache
    assert "3.3.3.3" in lookup._cache


def test_close_closes_reader(monkeypatch, tmp_path):
    fake_reader = _FakeReader({})
    monkeypatch.setattr(geoip_module.maxminddb, "open_database", lambda *_a, **_k: fake_reader)

    lookup = GeoIPLookup(tmp_path / "db.mmdb")
    lookup.close()

    assert fake_reader.closed is True


def test_close_on_missing_db_is_a_no_op(tmp_path):
    lookup = GeoIPLookup(tmp_path / "does-not-exist.mmdb")
    lookup.close()  # must not raise
