"""Source-only contract tests for the immutable Debian base image."""
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
DEBIAN_INDEX_DIGEST = "sha256:e2d08da6f42ef4b09b165d55528a12727aeed8240dc9edf888e3ec07e10ef9da"


def _validate(value: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VALIDATOR), value],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_debian_digest_validator_accepts_only_the_source_owned_amd64_lock() -> None:
    result = _validate(DEBIAN_AMD64_DIGEST)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"debian@{DEBIAN_AMD64_DIGEST}"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "debian:13.4",
        f"debian@{DEBIAN_AMD64_DIGEST}",
        "ubuntu@" + DEBIAN_AMD64_DIGEST,
        "sha256:" + "a" * 64,
        "sha256:" + "g" * 64,
        "sha256:" + "a" * 63,
    ],
)
def test_debian_digest_validator_rejects_missing_mutable_or_non_debian_refs(
    value: str,
) -> None:
    result = _validate(value)

    assert result.returncode != 0
    assert "DEBIAN_BASE_DIGEST" in result.stderr


def test_dockerfile_uses_the_official_debian_amd64_lock_for_every_debian_stage() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")

    assert f"ARG DEBIAN_BASE_DIGEST={DEBIAN_AMD64_DIGEST}" in text
    assert text.count("FROM --platform=linux/amd64 docker.io/library/debian:13.4@${DEBIAN_BASE_DIGEST}") == 2
    assert "FROM debian@${DEBIAN_BASE_DIGEST}" not in text
    assert "org.opencontainers.image.base.name=debian@${DEBIAN_BASE_DIGEST}" in text
    assert "org.opencontainers.image.base.digest=${DEBIAN_BASE_DIGEST}" in text


def test_docker_workflow_validates_and_binds_the_source_owned_debian_lock() -> None:
    text = DOCKER_WORKFLOW.read_text(encoding="utf-8")

    assert f"DEBIAN_BASE_DIGEST: {DEBIAN_AMD64_DIGEST}" in text
    assert DEBIAN_INDEX_DIGEST in text
    assert "HERMES_DEBIAN_BASE_DIGEST" not in text
    assert "platform: linux/amd64" in text
    assert "platform: linux/arm64" not in text
    assert "scripts/validate_debian_base.py" in text
    assert "DEBIAN_BASE_DIGEST=${{ env.DEBIAN_BASE_DIGEST }}" in text
    assert "org.opencontainers.image.base.name=debian@${{ env.DEBIAN_BASE_DIGEST }}" in text
    assert "org.opencontainers.image.base.digest=${{ env.DEBIAN_BASE_DIGEST }}" in text
