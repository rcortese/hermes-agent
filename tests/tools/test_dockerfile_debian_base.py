"""Source-only contract tests for immutable multiarch Debian base images."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "Dockerfile"
VALIDATOR = REPO_ROOT / "scripts" / "validate_debian_base.py"
DOCKER_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "docker.yml"
DEBIAN_AMD64_DIGEST = "sha256:de6a8f94c0e84f57a8e29769966b9d8c199b0891634280ad75ad804cf9827825"
DEBIAN_ARM64_DIGEST = "sha256:7e0ade45154451d0730a9818e9c4c8721ea4022e7c4dc1e42d44e99c5f4f1d04"
DEBIAN_INDEX_DIGEST = "sha256:e2d08da6f42ef4b09b165d55528a12727aeed8240dc9edf888e3ec07e10ef9da"
DEBIAN_DIGESTS = {"amd64": DEBIAN_AMD64_DIGEST, "arm64": DEBIAN_ARM64_DIGEST}


def _validate(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VALIDATOR), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_debian_digest_validator_reports_and_accepts_both_source_owned_locks() -> None:
    result = _validate()

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        f"linux/{arch} debian@{digest}" for arch, digest in DEBIAN_DIGESTS.items()
    ]
    for arch, digest in DEBIAN_DIGESTS.items():
        result = _validate(arch, digest)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == f"linux/{arch} debian@{digest}"


@pytest.mark.parametrize(
    ("arch", "value"),
    [
        ("amd64", DEBIAN_ARM64_DIGEST),
        ("arm64", DEBIAN_AMD64_DIGEST),
        ("amd64", ""),
        ("arm64", "debian:13.4"),
        ("amd64", f"debian@{DEBIAN_AMD64_DIGEST}"),
        ("arm64", "ubuntu@" + DEBIAN_ARM64_DIGEST),
        ("amd64", "sha256:" + "a" * 64),
        ("arm64", "sha256:" + "g" * 64),
        ("arm64", "sha256:" + "a" * 63),
    ],
)
def test_debian_digest_validator_rejects_missing_mutable_wrong_or_cross_arch_refs(
    arch: str, value: str
) -> None:
    result = _validate(arch, value)

    assert result.returncode != 0
    assert "DEBIAN_BASE_DIGESTS" in result.stderr


def test_dockerfile_selects_exact_debian_manifest_by_target_arch() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")

    assert f"ARG DEBIAN_BASE_AMD64_DIGEST={DEBIAN_AMD64_DIGEST}" in text
    assert f"ARG DEBIAN_BASE_ARM64_DIGEST={DEBIAN_ARM64_DIGEST}" in text
    for arch, digest in DEBIAN_DIGESTS.items():
        assert (
            f"FROM --platform=linux/{arch} "
            f"docker.io/library/debian:13.4@${{DEBIAN_BASE_{arch.upper()}_DIGEST}} "
            f"AS debian_{arch}"
        ) in text
        assert f"LABEL org.opencontainers.image.base.name=debian@${{DEBIAN_BASE_{arch.upper()}_DIGEST}}" in text
        assert f"LABEL org.opencontainers.image.base.digest=${{DEBIAN_BASE_{arch.upper()}_DIGEST}}" in text
    assert text.count("FROM debian_${TARGETARCH}") == 2
    assert "FROM --platform=linux/amd64 docker.io/library/debian:13.4@${DEBIAN_BASE_DIGEST}" not in text
    assert "DEBIAN_BASE_DIGEST=" not in text


def test_docker_workflow_binds_both_architectures_and_exact_provenance_labels() -> None:
    text = DOCKER_WORKFLOW.read_text(encoding="utf-8")

    assert DEBIAN_INDEX_DIGEST in text
    for arch, digest in DEBIAN_DIGESTS.items():
        assert f"DEBIAN_BASE_{arch.upper()}_DIGEST: {digest}" in text
        assert f"- arch: {arch}" in text
        assert f"platform: linux/{arch}" in text
        assert f"debian-base-digest: {digest}" in text
        assert (
            f"scripts/validate_debian_base.py {arch} "
            f'"${{DEBIAN_BASE_{arch.upper()}_DIGEST}}"'
        ) in text
    assert text.count("org.opencontainers.image.base.name=debian@${{ matrix.debian-base-digest }}") == 2
    assert text.count("org.opencontainers.image.base.digest=${{ matrix.debian-base-digest }}") == 2
    assert "HERMES_DEBIAN_BASE_DIGEST" not in text
    assert "DEBIAN_BASE_DIGEST=" not in text
