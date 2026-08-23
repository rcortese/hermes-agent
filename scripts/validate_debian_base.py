#!/usr/bin/env python3
"""Validate the source-owned Debian 13.4 linux/amd64 Docker base lock.

The official Docker registry resolved this platform manifest on 2026-08-23;
the companion multi-platform index is retained below as provenance. This
script deliberately never contacts a registry.
"""
from __future__ import annotations

import sys

DEBIAN_BASE_DIGEST = "sha256:de6a8f94c0e84f57a8e29769966b9d8c199b0891634280ad75ad804cf9827825"
DEBIAN_BASE_INDEX_DIGEST = "sha256:e2d08da6f42ef4b09b165d55528a12727aeed8240dc9edf888e3ec07e10ef9da"
DEBIAN_BASE_PLATFORM = "linux/amd64"


def main(argv: list[str]) -> int:
    value = argv[1] if len(argv) == 2 else ""
    if value != DEBIAN_BASE_DIGEST:
        print(
            "DEBIAN_BASE_DIGEST must equal the source-owned Debian 13.4 "
            f"{DEBIAN_BASE_PLATFORM} manifest {DEBIAN_BASE_DIGEST} "
            f"(Docker Official Images index {DEBIAN_BASE_INDEX_DIGEST}).",
            file=sys.stderr,
        )
        return 2
    print(f"debian@{value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
