"""GeoIP/ASN lookup (`docs/implementation_plan.md` §4.1, FR-013/FR-013a).

Reads a local MaxMind-format `.mmdb` file at `geoip.db_path` once at startup;
the AAC never fetches or refreshes this file itself
(`scripts/update_geoip_db.py` is the separate, standalone command for that —
its real download/license-key flow is explicitly parked, see
`docs/open_tbd.md`).

The real deployment path for `db_path` isn't decided yet (`docs/open_tbd.md`),
so a missing/unreadable file **fails open**: country/ASN become an optional,
degraded signal (every lookup returns `(None, None)`) rather than a startup
crash — the same posture as `app.auth`'s Keycloak-unreachable handling.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import maxminddb

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 300
_CACHE_MAX_SIZE = 10_000


class GeoIPLookup:
    def __init__(self, db_path: Path) -> None:
        self._cache: dict[str, tuple[tuple[str | None, str | None], float]] = {}
        try:
            self._reader: maxminddb.Reader | None = maxminddb.open_database(str(db_path))
        except (FileNotFoundError, maxminddb.InvalidDatabaseError) as exc:
            logger.warning(
                "geoip_db_unavailable", extra={"db_path": str(db_path), "error": str(exc)}
            )
            self._reader = None

    def lookup(self, ip: str) -> tuple[str | None, str | None]:
        """Returns `(country, asn)`; either/both are `None` when the loaded
        database doesn't carry that field (e.g. a City DB has no ASN, an ASN
        DB has no country — see `docs/open_tbd.md`) or no db is loaded."""
        if self._reader is None:
            return None, None

        now = time.monotonic()
        cached = self._cache.get(ip)
        if cached is not None and now - cached[1] < _CACHE_TTL_SECONDS:
            return cached[0]

        record = self._reader.get(ip)
        result = _extract(record) if isinstance(record, dict) else (None, None)

        if len(self._cache) >= _CACHE_MAX_SIZE:
            oldest_ip = min(self._cache, key=lambda k: self._cache[k][1])
            del self._cache[oldest_ip]
        self._cache[ip] = (result, now)
        return result

    def close(self) -> None:
        if self._reader is not None:
            self._reader.close()


def _extract(record: dict) -> tuple[str | None, str | None]:
    country = record.get("country", {}).get("iso_code")
    asn = record.get("autonomous_system_number")
    return country, (str(asn) if asn is not None else None)
