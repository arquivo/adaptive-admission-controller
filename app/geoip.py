"""GeoIP/ASN lookup (`docs/implementation_plan.md` §4.1, FR-013/FR-013a).

Reads two local MaxMind-format `.mmdb` files once at startup —
`geoip.city_db_path` (country) and `geoip.asn_db_path` (ASN). MaxMind's free
GeoLite2 tier ships these as two separate files (no free combined
country+ASN database), so each is opened independently. The AAC never
fetches or refreshes either file itself (`scripts/update_geoip_db.py` is the
separate, standalone command for that, one edition per invocation).

The real deployment paths for `city_db_path`/`asn_db_path` are
installation-dependent (`docs/open_tbd.md`), so each file **independently
fails open**: a missing/unreadable city db leaves `country` `None` while
`asn` lookups still work from the asn db (and vice versa) — the same
posture as `app.auth`'s Keycloak-unreachable handling, just applied
per-file rather than crashing startup.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import maxminddb

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 300
_CACHE_MAX_SIZE = 10_000


def _open(db_path: Path) -> maxminddb.Reader | None:
    try:
        return maxminddb.open_database(str(db_path))
    except (FileNotFoundError, maxminddb.InvalidDatabaseError) as exc:
        logger.warning("geoip_db_unavailable", extra={"db_path": str(db_path), "error": str(exc)})
        return None


class GeoIPLookup:
    def __init__(self, city_db_path: Path, asn_db_path: Path) -> None:
        self._cache: dict[str, tuple[tuple[str | None, str | None], float]] = {}
        self._city_reader = _open(city_db_path)
        self._asn_reader = _open(asn_db_path)

    def lookup(self, ip: str) -> tuple[str | None, str | None]:
        """Returns `(country, asn)`; either/both are `None` when the
        corresponding reader has no db loaded or has no record for `ip`."""
        if self._city_reader is None and self._asn_reader is None:
            return None, None

        now = time.monotonic()
        cached = self._cache.get(ip)
        if cached is not None and now - cached[1] < _CACHE_TTL_SECONDS:
            return cached[0]

        country = None
        if self._city_reader is not None:
            record = self._city_reader.get(ip)
            if isinstance(record, dict):
                country = record.get("country", {}).get("iso_code")

        asn = None
        if self._asn_reader is not None:
            record = self._asn_reader.get(ip)
            if isinstance(record, dict):
                asn_number = record.get("autonomous_system_number")
                asn = str(asn_number) if asn_number is not None else None

        result = (country, asn)
        if len(self._cache) >= _CACHE_MAX_SIZE:
            oldest_ip = min(self._cache, key=lambda k: self._cache[k][1])
            del self._cache[oldest_ip]
        self._cache[ip] = (result, now)
        return result

    def close(self) -> None:
        if self._city_reader is not None:
            self._city_reader.close()
        if self._asn_reader is not None:
            self._asn_reader.close()
