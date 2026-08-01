"""Unit tests for app.geoip — GeoIP/ASN lookup (FR-013/FR-013a).

No real MaxMind `.mmdb` file is used: the missing-file fail-open path is
exercised for real (`maxminddb.open_database` genuinely raises
`FileNotFoundError`), while the lookup/cache paths substitute a fake reader
via monkeypatching `app.geoip.maxminddb.open_database` — this project ships
no MaxMind database writer to build a real fixture file with.

Two independent readers (city db for country, asn db for ASN) are opened
per `GeoIPLookup` instance — most tests here use the same fake reader for
both positions since they don't care which file is which; tests that
specifically exercise per-file fail-open behavior use `_open_side_effect`
to give city/asn paths distinct fakes.
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


def _open_side_effect(readers: dict[str, object]):
    """Maps `str(db_path)` to a fake reader (or a raised FileNotFoundError
    for paths not present), so city/asn paths can behave independently."""

    def _open(path_str, *_a, **_k):
        if path_str not in readers:
            raise FileNotFoundError(path_str)
        return readers[path_str]

    return _open


def test_missing_db_files_fail_open(tmp_path, caplog):
    missing_city = tmp_path / "does-not-exist-city.mmdb"
    missing_asn = tmp_path / "does-not-exist-asn.mmdb"

    with caplog.at_level("WARNING"):
        lookup = GeoIPLookup(missing_city, missing_asn)

    assert lookup.lookup("1.2.3.4") == (None, None)
    assert caplog.text.count("geoip_db_unavailable") == 2


def test_invalid_db_files_fail_open(monkeypatch, tmp_path):
    def _raise(*_a, **_k):
        raise maxminddb.InvalidDatabaseError("corrupt")

    monkeypatch.setattr(geoip_module.maxminddb, "open_database", _raise)

    lookup = GeoIPLookup(tmp_path / "corrupt-city.mmdb", tmp_path / "corrupt-asn.mmdb")

    assert lookup.lookup("1.2.3.4") == (None, None)


def test_lookup_extracts_country_and_asn(monkeypatch, tmp_path):
    fake_reader = _FakeReader(
        {"1.2.3.4": {"country": {"iso_code": "PT"}, "autonomous_system_number": 64500}}
    )
    monkeypatch.setattr(geoip_module.maxminddb, "open_database", lambda *_a, **_k: fake_reader)

    lookup = GeoIPLookup(tmp_path / "city.mmdb", tmp_path / "asn.mmdb")

    assert lookup.lookup("1.2.3.4") == ("PT", "64500")


def test_lookup_missing_fields_return_none(monkeypatch, tmp_path):
    """An ASN-only database has no `country` key; a City-only database has no
    `autonomous_system_number` — either/both may legitimately be absent."""
    fake_reader = _FakeReader({"1.2.3.4": {"autonomous_system_number": 64500}})
    monkeypatch.setattr(geoip_module.maxminddb, "open_database", lambda *_a, **_k: fake_reader)

    lookup = GeoIPLookup(tmp_path / "city.mmdb", tmp_path / "asn.mmdb")

    assert lookup.lookup("1.2.3.4") == (None, "64500")


def test_lookup_unknown_ip_returns_none(monkeypatch, tmp_path):
    fake_reader = _FakeReader({})
    monkeypatch.setattr(geoip_module.maxminddb, "open_database", lambda *_a, **_k: fake_reader)

    lookup = GeoIPLookup(tmp_path / "city.mmdb", tmp_path / "asn.mmdb")

    assert lookup.lookup("9.9.9.9") == (None, None)


def test_city_reader_failure_leaves_asn_working(tmp_path, monkeypatch):
    city_path = tmp_path / "city.mmdb"
    asn_path = tmp_path / "asn.mmdb"
    asn_reader = _FakeReader({"1.2.3.4": {"autonomous_system_number": 64500}})

    monkeypatch.setattr(
        geoip_module.maxminddb,
        "open_database",
        _open_side_effect({str(asn_path): asn_reader}),
    )

    lookup = GeoIPLookup(city_path, asn_path)

    assert lookup.lookup("1.2.3.4") == (None, "64500")


def test_asn_reader_failure_leaves_country_working(tmp_path, monkeypatch):
    city_path = tmp_path / "city.mmdb"
    asn_path = tmp_path / "asn.mmdb"
    city_reader = _FakeReader({"1.2.3.4": {"country": {"iso_code": "PT"}}})

    monkeypatch.setattr(
        geoip_module.maxminddb,
        "open_database",
        _open_side_effect({str(city_path): city_reader}),
    )

    lookup = GeoIPLookup(city_path, asn_path)

    assert lookup.lookup("1.2.3.4") == ("PT", None)


def test_both_readers_missing_fails_open(tmp_path):
    missing_city = tmp_path / "does-not-exist-city.mmdb"
    missing_asn = tmp_path / "does-not-exist-asn.mmdb"
    lookup = GeoIPLookup(missing_city, missing_asn)

    assert lookup.lookup("1.2.3.4") == (None, None)
    lookup.close()  # must not raise


def test_repeated_lookup_hits_cache_not_reader(monkeypatch, tmp_path):
    fake_reader = _FakeReader({"1.2.3.4": {"country": {"iso_code": "PT"}}})
    monkeypatch.setattr(geoip_module.maxminddb, "open_database", lambda *_a, **_k: fake_reader)

    lookup = GeoIPLookup(tmp_path / "city.mmdb", tmp_path / "asn.mmdb")
    lookup.lookup("1.2.3.4")
    lookup.lookup("1.2.3.4")
    lookup.lookup("1.2.3.4")

    # Same fake reader is used for both city/asn positions, so each is
    # queried once per lookup() call — 2 calls for the first (uncached)
    # lookup, 0 for the two cached repeats.
    assert fake_reader.get_calls == 2


def test_expired_cache_entry_re_queries_reader(monkeypatch, tmp_path):
    fake_reader = _FakeReader({"1.2.3.4": {"country": {"iso_code": "PT"}}})
    monkeypatch.setattr(geoip_module.maxminddb, "open_database", lambda *_a, **_k: fake_reader)
    monkeypatch.setattr(geoip_module, "_CACHE_TTL_SECONDS", 0)

    lookup = GeoIPLookup(tmp_path / "city.mmdb", tmp_path / "asn.mmdb")
    lookup.lookup("1.2.3.4")
    lookup.lookup("1.2.3.4")

    assert fake_reader.get_calls == 4


def test_cache_evicts_oldest_entry_at_size_cap(monkeypatch, tmp_path):
    fake_reader = _FakeReader({})
    monkeypatch.setattr(geoip_module.maxminddb, "open_database", lambda *_a, **_k: fake_reader)
    monkeypatch.setattr(geoip_module, "_CACHE_MAX_SIZE", 2)

    lookup = GeoIPLookup(tmp_path / "city.mmdb", tmp_path / "asn.mmdb")
    lookup.lookup("1.1.1.1")
    lookup.lookup("2.2.2.2")
    lookup.lookup("3.3.3.3")  # evicts 1.1.1.1, the oldest

    assert "1.1.1.1" not in lookup._cache
    assert "2.2.2.2" in lookup._cache
    assert "3.3.3.3" in lookup._cache


def test_close_closes_both_readers(monkeypatch, tmp_path):
    city_path = tmp_path / "city.mmdb"
    asn_path = tmp_path / "asn.mmdb"
    city_reader = _FakeReader({})
    asn_reader = _FakeReader({})

    monkeypatch.setattr(
        geoip_module.maxminddb,
        "open_database",
        _open_side_effect({str(city_path): city_reader, str(asn_path): asn_reader}),
    )

    lookup = GeoIPLookup(city_path, asn_path)
    lookup.close()

    assert city_reader.closed is True
    assert asn_reader.closed is True


def test_close_on_missing_dbs_is_a_no_op(tmp_path):
    missing_city = tmp_path / "does-not-exist-city.mmdb"
    missing_asn = tmp_path / "does-not-exist-asn.mmdb"
    lookup = GeoIPLookup(missing_city, missing_asn)
    lookup.close()  # must not raise
