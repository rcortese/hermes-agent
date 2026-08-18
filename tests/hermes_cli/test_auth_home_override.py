from pathlib import Path

from hermes_cli import auth


def test_auth_file_path_prefers_explicit_shared_home(monkeypatch, tmp_path: Path) -> None:
    profile_home = tmp_path / "profile"
    shared_home = tmp_path / "shared-auth"
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.setenv("HERMES_AUTH_HOME", str(shared_home))

    assert auth._auth_file_path() == shared_home / "auth.json"


def test_auth_file_path_falls_back_to_active_hermes_home(monkeypatch, tmp_path: Path) -> None:
    profile_home = tmp_path / "profile"
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.delenv("HERMES_AUTH_HOME", raising=False)

    assert auth._auth_file_path() == profile_home / "auth.json"
