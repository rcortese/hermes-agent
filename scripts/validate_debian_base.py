#!/usr/bin/env python3
"""Validate source-owned Debian 13.4 Linux base-manifest locks.

The official Docker registry resolved these platform manifests on 2026-08-23;
the companion multi-platform index is retained below as provenance. This
script deliberately never contacts a registry.
"""
from __future__ import annotations

import sys

DEBIAN_BASE_INDEX_DIGEST = "sha256:e2d08da6f42ef4b09b165d55528a12727aeed8240dc9edf888e3ec07e10ef9da"
DEBIAN_BASE_DIGESTS = {
    "amd64": "sha256:de6a8f94c0e84f57a8e29769966b9d8c199b0891634280ad75ad804cf9827825",
    "arm64": "sha256:7e0ade45154451d0730a9818e9c4c8721ea4022e7c4dc1e42d44e99c5f4f1d04",
}


def main(argv: list[str]) -> int:
    if len(argv) == 1:
        for arch, digest in DEBIAN_BASE_DIGESTS.items():
            print(f"linux/{arch} debian@{digest}")
        return 0
    if len(argv) == 3 and argv[1] in DEBIAN_BASE_DIGESTS:
        arch, value = argv[1:]
        if value == DEBIAN_BASE_DIGESTS[arch]:
            print(f"linux/{arch} debian@{value}")
            return 0
    print(
        "DEBIAN_BASE_DIGESTS must retain the source-owned Debian 13.4 "
        "linux/amd64 and linux/arm64 manifests from Docker Official Images "
        f"index {DEBIAN_BASE_INDEX_DIGEST}. Usage: "
        "validate_debian_base.py [amd64|arm64 sha256:<digest>]",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
