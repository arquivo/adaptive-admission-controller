"""Unit tests for scripts.update_geoip_db — MaxMind GeoLite2 download (FR-013a).

No real MaxMind HTTP call or `.mmdb` fixture is used, matching this repo's
existing testing philosophy (see tests/unit/test_geoip.py): the exact I/O
boundaries (`_download`, `maxminddb.open_database`) are monkeypatched
directly rather than pulling in a new HTTP-mocking dependency. `_extract_mmdb`
is exercised for real against small in-memory tarballs built with the
stdlib `tarfile` module.
"""

from __future__ import annotations

import io
import tarfile

import maxminddb
import pytest

import scripts.update_geoip_db as update_geoip_db


def _make_tar_gz(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


class _FakeMetadata:
    def __init__(self, database_type: str, build_epoch: int = 1700000000):
        self.database_type = database_type
        self.build_epoch = build_epoch


class _FakeReader:
    def __init__(self, database_type: str):
        self._metadata = _FakeMetadata(database_type)

    def metadata(self):
        return self._metadata

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def test_missing_credentials_returns_exit_code(monkeypatch, capsys):
    monkeypatch.delenv("MAXMIND_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("MAXMIND_LICENSE_KEY", raising=False)
    monkeypatch.setattr(
        "sys.argv",
        ["update_geoip_db.py", "--edition", "GeoLite2-City", "--dest-path", "/tmp/x.mmdb"],
    )

    assert update_geoip_db.main() == update_geoip_db.EXIT_MISSING_CREDENTIALS
    assert "MAXMIND_ACCOUNT_ID" in capsys.readouterr().err


def test_auth_failure_returns_exit_code(monkeypatch):
    import httpx

    monkeypatch.setenv("MAXMIND_ACCOUNT_ID", "acc")
    monkeypatch.setenv("MAXMIND_LICENSE_KEY", "key")
    monkeypatch.setattr(
        "sys.argv",
        ["update_geoip_db.py", "--edition", "GeoLite2-City", "--dest-path", "/tmp/x.mmdb"],
    )

    def _raise(*_a, **_k):
        request = httpx.Request("GET", "https://download.maxmind.com/x")
        response = httpx.Response(401, request=request)
        raise httpx.HTTPStatusError("unauthorized", request=request, response=response)

    monkeypatch.setattr(update_geoip_db, "_download", _raise)

    assert update_geoip_db.main() == update_geoip_db.EXIT_AUTH_FAILED


def test_download_network_error_returns_exit_code(monkeypatch):
    import httpx

    monkeypatch.setenv("MAXMIND_ACCOUNT_ID", "acc")
    monkeypatch.setenv("MAXMIND_LICENSE_KEY", "key")
    monkeypatch.setattr(
        "sys.argv",
        ["update_geoip_db.py", "--edition", "GeoLite2-City", "--dest-path", "/tmp/x.mmdb"],
    )

    def _raise(*_a, **_k):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(update_geoip_db, "_download", _raise)

    assert update_geoip_db.main() == update_geoip_db.EXIT_DOWNLOAD_FAILED


def test_extract_mmdb_rejects_zero_members():
    tar_bytes = _make_tar_gz({"COPYRIGHT.txt": b"copyright"})

    with pytest.raises(ValueError, match="found 0"):
        update_geoip_db._extract_mmdb(tar_bytes)


def test_extract_mmdb_rejects_multiple_members():
    tar_bytes = _make_tar_gz({"a.mmdb": b"aaa", "b.mmdb": b"bbb"})

    with pytest.raises(ValueError, match="found 2"):
        update_geoip_db._extract_mmdb(tar_bytes)


def test_extract_mmdb_returns_single_member_bytes():
    tar_bytes = _make_tar_gz({"GeoLite2-City_20240101/GeoLite2-City.mmdb": b"fake-mmdb-bytes"})

    assert update_geoip_db._extract_mmdb(tar_bytes) == b"fake-mmdb-bytes"


def test_bad_archive_returns_exit_code(monkeypatch, tmp_path):
    dest_path = tmp_path / "x.mmdb"
    monkeypatch.setenv("MAXMIND_ACCOUNT_ID", "acc")
    monkeypatch.setenv("MAXMIND_LICENSE_KEY", "key")
    monkeypatch.setattr(
        "sys.argv",
        ["update_geoip_db.py", "--edition", "GeoLite2-City", "--dest-path", str(dest_path)],
    )
    monkeypatch.setattr(
        update_geoip_db, "_download", lambda *_a, **_k: _make_tar_gz({"COPYRIGHT.txt": b"c"})
    )

    assert update_geoip_db.main() == update_geoip_db.EXIT_BAD_ARCHIVE


def test_validate_rejects_corrupt_bytes(tmp_path):
    with pytest.raises(maxminddb.InvalidDatabaseError):
        update_geoip_db._validate(b"not a real mmdb file", "GeoLite2-City", tmp_path)


def test_validate_rejects_edition_mismatch(monkeypatch, tmp_path):
    monkeypatch.setattr(
        update_geoip_db.maxminddb,
        "open_database",
        lambda *_a, **_k: _FakeReader("GeoLite2-ASN"),
    )

    with pytest.raises(ValueError, match="does not match"):
        update_geoip_db._validate(b"irrelevant", "GeoLite2-City", tmp_path)


def test_validate_cleans_up_probe_file_on_success(monkeypatch, tmp_path):
    monkeypatch.setattr(
        update_geoip_db.maxminddb,
        "open_database",
        lambda *_a, **_k: _FakeReader("GeoLite2-City"),
    )

    update_geoip_db._validate(b"irrelevant", "GeoLite2-City", tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_atomic_write_replaces_destination(tmp_path):
    dest_path = tmp_path / "GeoLite2-City.mmdb"
    dest_path.write_bytes(b"old-bytes")

    update_geoip_db._atomic_write(b"new-bytes", dest_path)

    assert dest_path.read_bytes() == b"new-bytes"
    assert list(tmp_path.iterdir()) == [dest_path]  # no leftover temp file


def test_successful_full_flow(monkeypatch, tmp_path):
    dest_path = tmp_path / "GeoLite2-City.mmdb"
    monkeypatch.setenv("MAXMIND_ACCOUNT_ID", "acc")
    monkeypatch.setenv("MAXMIND_LICENSE_KEY", "key")
    monkeypatch.setattr(
        "sys.argv",
        ["update_geoip_db.py", "--edition", "GeoLite2-City", "--dest-path", str(dest_path)],
    )
    tar_bytes = _make_tar_gz({"GeoLite2-City_20240101/GeoLite2-City.mmdb": b"real-bytes"})
    monkeypatch.setattr(update_geoip_db, "_download", lambda *_a, **_k: tar_bytes)
    monkeypatch.setattr(
        update_geoip_db.maxminddb,
        "open_database",
        lambda *_a, **_k: _FakeReader("GeoLite2-City"),
    )

    assert update_geoip_db.main() == update_geoip_db.EXIT_OK
    assert dest_path.read_bytes() == b"real-bytes"


def test_edition_mismatch_leaves_destination_untouched(monkeypatch, tmp_path):
    dest_path = tmp_path / "GeoLite2-City.mmdb"
    dest_path.write_bytes(b"sentinel")
    monkeypatch.setenv("MAXMIND_ACCOUNT_ID", "acc")
    monkeypatch.setenv("MAXMIND_LICENSE_KEY", "key")
    monkeypatch.setattr(
        "sys.argv",
        ["update_geoip_db.py", "--edition", "GeoLite2-City", "--dest-path", str(dest_path)],
    )
    monkeypatch.setattr(
        update_geoip_db,
        "_download",
        lambda *_a, **_k: _make_tar_gz({"GeoLite2-City_20240101/GeoLite2-City.mmdb": b"bad-bytes"}),
    )
    monkeypatch.setattr(
        update_geoip_db.maxminddb,
        "open_database",
        lambda *_a, **_k: _FakeReader("GeoLite2-ASN"),
    )

    assert update_geoip_db.main() == update_geoip_db.EXIT_INVALID_DATABASE
    assert dest_path.read_bytes() == b"sentinel"
    assert list(tmp_path.iterdir()) == [dest_path]  # no leftover probe/temp files
