#!/usr/bin/env python3
"""Standalone GeoIP/ASN database refresh command (FR-013a).

Run manually or by deployment automation — NEVER invoked by the AAC process
itself. The AAC only picks up a refreshed database on its next restart.

Phase 1 stub: the real MaxMind GeoLite2 download/license-key flow is a
Phase 3 concern (docs/open_tbd.md — the refresh mechanism is explicitly
parked pending an account/license-key decision).
"""

from __future__ import annotations

import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-path",
        required=True,
        help=(
            "Destination path for the refreshed GeoLite2 database "
            "(geoip.db_path in config/backends.yaml)"
        ),
    )
    parser.parse_args()

    raise NotImplementedError(
        "GeoIP refresh mechanism not yet implemented — see docs/open_tbd.md "
        "(MaxMind account/license-key decision pending)."
    )


if __name__ == "__main__":
    sys.exit(main())
