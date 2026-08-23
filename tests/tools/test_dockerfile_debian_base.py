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
VALID_DIGEST = "sha256:" + "a" * 64


def _validate(value: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VALIDATOR), value],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_debian_digest_validator_accepts_only_a_digest_shaped_value() -> None:
    result = _validate(VALID_DIGEST)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"debian@{VALID_DIGEST}"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "debian:13.4",
        f"debian@{VALID_DIGEST}",
        "ubuntu@" + VALID_DIGEST,
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


def test_dockerfile_uses_required_debian_digest_for_every_debian_stage() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")

    assert "ARG DEBIAN_BASE_DIGEST" in text
    assert text.count("FROM debian@${DEBIAN_BASE_DIGEST}") == 2
    assert "FROM debian:13.4" not in text
    assert "org.opencontainers.image.base.name=debian@${DEBIAN_BASE_DIGEST}" in text
    assert "org.opencontainers.image.base.digest=${DEBIAN_BASE_DIGEST}" in text


def test_docker_workflow_validates_and_binds_the_supplied_debian_digest() -> None:
    text = DOCKER_WORKFLOW.read_text(encoding="utf-8")

    assert "HERMES_DEBIAN_BASE_DIGEST" in text
    assert "scripts/validate_debian_base.py" in text
    assert "DEBIAN_BASE_DIGEST=${{ env.DEBIAN_BASE_DIGEST }}" in text
    assert "org.opencontainers.image.base.name=debian@${{ env.DEBIAN_BASE_DIGEST }}" in text
    assert "org.opencontainers.image.base.digest=${{ env.DEBIAN_BASE_DIGEST }}" in text
