#!/usr/bin/env python3
"""Validate the supplied immutable Debian base digest for Docker builds.

This verifies only the source-side shape and repository binding.  Registry
resolution and provenance verification belong to the admission environment;
this script deliberately never contacts a registry or invents a digest.
"""
from __future__ import annotations

import re
import sys

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


def main(argv: list[str]) -> int:
    value = argv[1] if len(argv) == 2 else ""
    if not _DIGEST.fullmatch(value):
        print(
            "DEBIAN_BASE_DIGEST must be a supplied sha256:<64 lowercase-hex> "
            "digest for the fixed debian repository; tags and image references "
            "are not accepted.",
            file=sys.stderr,
        )
        return 2
    print(f"debian@{value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
