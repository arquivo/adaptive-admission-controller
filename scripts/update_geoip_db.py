#!/usr/bin/env python3
"""Standalone GeoIP/ASN database refresh command (FR-013a).

Run manually or by deployment automation — NEVER invoked by the AAC process
itself. The AAC only picks up a refreshed database on its next restart.

Downloads one MaxMind GeoLite2 edition per invocation from MaxMind's direct
download API (https://dev.maxmind.com/geoip/updating-databases), using HTTP
Basic Auth with credentials from the `MAXMIND_ACCOUNT_ID`/
`MAXMIND_LICENSE_KEY` environment variables. Deployment automation runs this
twice per refresh cycle — once for `GeoLite2-City`, once for `GeoLite2-ASN`
— since the free tier ships no combined country+ASN file.

MaxMind's API documents no checksum-verification endpoint, so the
downloaded file is instead validated by opening it with
`maxminddb.open_database()` and checking its `database_type` matches the
requested edition.
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import httpx
import maxminddb

_DOWNLOAD_URL = "https://download.maxmind.com/geoip/databases/{edition}/download?suffix=tar.gz"

EXIT_OK = 0
EXIT_MISSING_CREDENTIALS = 2
EXIT_AUTH_FAILED = 3
EXIT_DOWNLOAD_FAILED = 4
EXIT_BAD_ARCHIVE = 5
EXIT_INVALID_DATABASE = 6


def _download(edition: str, account_id: str, license_key: str) -> bytes:
    url = _DOWNLOAD_URL.format(edition=edition)
    with httpx.Client(follow_redirects=True, timeout=60.0) as client:
        response = client.get(url, auth=(account_id, license_key))
        response.raise_for_status()
        return response.content


def _extract_mmdb(tar_bytes: bytes) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tar:
        members = [m for m in tar.getmembers() if m.name.endswith(".mmdb")]
        if len(members) != 1:
            raise ValueError(f"expected exactly one .mmdb file in archive, found {len(members)}")
        extracted = tar.extractfile(members[0])
        if extracted is None:
            raise ValueError("could not read .mmdb member from archive")
        return extracted.read()


def _validate(mmdb_bytes: bytes, edition: str, tmp_dir: Path) -> maxminddb.reader.Metadata:
    with tempfile.NamedTemporaryFile(dir=tmp_dir, suffix=".mmdb", delete=False) as probe_f:
        probe_f.write(mmdb_bytes)
        probe_path = Path(probe_f.name)
    try:
        with maxminddb.open_database(str(probe_path)) as reader:
            metadata = reader.metadata()
        if metadata.database_type != edition:
            raise ValueError(
                f"downloaded database type {metadata.database_type!r} does not match "
                f"requested edition {edition!r}"
            )
        return metadata
    finally:
        probe_path.unlink()


def _atomic_write(mmdb_bytes: bytes, dest_path: Path) -> None:
    with tempfile.NamedTemporaryFile(dir=dest_path.parent, suffix=".mmdb", delete=False) as tmp_f:
        tmp_f.write(mmdb_bytes)
        tmp_path = Path(tmp_f.name)
    try:
        os.replace(tmp_path, dest_path)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--edition",
        required=True,
        choices=["GeoLite2-City", "GeoLite2-ASN"],
        help="MaxMind edition ID to download.",
    )
    parser.add_argument(
        "--dest-path",
        required=True,
        type=Path,
        help="Destination .mmdb path (geoip.city_db_path or geoip.asn_db_path).",
    )
    args = parser.parse_args()

    account_id = os.environ.get("MAXMIND_ACCOUNT_ID")
    license_key = os.environ.get("MAXMIND_LICENSE_KEY")
    if not account_id or not license_key:
        print(
            "error: MAXMIND_ACCOUNT_ID and MAXMIND_LICENSE_KEY environment variables "
            "are required",
            file=sys.stderr,
        )
        return EXIT_MISSING_CREDENTIALS

    try:
        tar_bytes = _download(args.edition, account_id, license_key)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (401, 403):
            print(
                f"error: MaxMind authentication failed ({exc.response.status_code})",
                file=sys.stderr,
            )
            return EXIT_AUTH_FAILED
        print(f"error: MaxMind download failed: {exc}", file=sys.stderr)
        return EXIT_DOWNLOAD_FAILED
    except httpx.HTTPError as exc:
        print(f"error: MaxMind download failed: {exc}", file=sys.stderr)
        return EXIT_DOWNLOAD_FAILED

    try:
        mmdb_bytes = _extract_mmdb(tar_bytes)
    except (tarfile.TarError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_BAD_ARCHIVE

    try:
        metadata = _validate(mmdb_bytes, args.edition, args.dest_path.parent)
    except (maxminddb.InvalidDatabaseError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INVALID_DATABASE

    build_time = datetime.fromtimestamp(metadata.build_epoch, tz=UTC).isoformat()
    print(f"downloaded {args.edition}: build_epoch={metadata.build_epoch} ({build_time})")

    _atomic_write(mmdb_bytes, args.dest_path)
    print(f"wrote {args.dest_path}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
