"""Synthetic account tests; real shared flock, no provider/network access."""
import json
import multiprocessing
from pathlib import Path

import pytest
import agent.credential_pool as cp
import hermes_cli.auth as auth

PROVIDER = "openai-codex"


def _row(id_, access="a", refresh="r"):
    return dict(id=id_, source="manual:device_code", auth_type="oauth",
                access_token=f"{access}-{id_}", refresh_token=f"{refresh}-{id_}", priority=0)


def _seed(path, rows):
    path.write_text(json.dumps({"credential_pool": {PROVIDER: rows}}))


def _rows(path):
    return {r["id"]: r for r in json.loads(path.read_text())["credential_pool"][PROVIDER]}


def _pool_from_disk(path, cred_id="A"):
    return cp.CredentialPool(PROVIDER, [cp.PooledCredential.from_dict(PROVIDER, _rows(path)[cred_id])])


@pytest.fixture
def env(tmp_path, monkeypatch):
    for var in ("HOME", "HERMES_HOME", "HERMES_AUTH_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(var, str(tmp_path))
    auth_file, lock_file = tmp_path / "auth.json", tmp_path / "auth.lock"
    monkeypatch.setattr(auth, "_auth_file_path", lambda: auth_file)
    # Upstream locks are target-path scoped; _auth_file_path selects the synthetic lock.
    monkeypatch.setattr(auth, "_global_auth_file_path", lambda: None)
    monkeypatch.setattr(cp, "get_pool_strategy", lambda *a, **k: "round_robin")
    _seed(auth_file, [_row("A"), _row("B")])
    return auth_file


@pytest.fixture
def fake_refresh(monkeypatch):
    calls = []
    def fake(*a, **k):
        calls.append(a)
        return dict(access_token="new-a", refresh_token="new-r", last_refresh="2026-01-01T00:00:00Z")
    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", fake)
    return calls


def test_concurrent_refresh_dedup(env, fake_refresh):
    pool1, pool2 = _pool_from_disk(env), _pool_from_disk(env)
    r1 = pool1._refresh_entry(pool1.entries()[0], force=True)
    r2 = pool2._refresh_entry(pool2.entries()[0], force=True)
    assert fake_refresh == [("a-A", "r-A")]
    assert r1.access_token == r2.access_token == "new-a"
    assert r1.refresh_token == r2.refresh_token == "new-r"


def test_refresh_leaves_peer_intact(env, fake_refresh):
    before = _rows(env)["B"]
    pool = _pool_from_disk(env)
    pool._refresh_entry(pool.entries()[0], force=True)
    assert _rows(env)["B"] == before
    assert not json.loads(env.read_text()).get("providers", {}).get(PROVIDER)


def test_mark_exhausted_preserves_peer_refresh(env, fake_refresh):
    pool1, pool2 = _pool_from_disk(env), _pool_from_disk(env)
    original = pool2.entries()[0]
    pool1._refresh_entry(pool1.entries()[0], force=True)
    pool2._mark_exhausted(original, 429)
    row = _rows(env)["A"]
    assert (row["access_token"], row["refresh_token"]) == ("new-a", "new-r")
    assert row["last_error_code"] == 429
    assert pool2.entries()[0].access_token == "new-a"


@pytest.mark.parametrize("field", ["access_token", "refresh_token"])
def test_stale_force_refresh_skips_when_already_fresh(env, fake_refresh, field):
    pool = _pool_from_disk(env)
    original = pool.entries()[0]
    data = json.loads(env.read_text())
    data["credential_pool"][PROVIDER][0][field] = f"peer-{field}"
    env.write_text(json.dumps(data))
    result = pool._refresh_entry(original, force=True)
    assert not fake_refresh
    assert getattr(result, field) == f"peer-{field}"


def test_removed_credential_refresh_returns_none(env, fake_refresh):
    pool = _pool_from_disk(env)
    original = pool.entries()[0]
    _seed(env, [_row("B")])
    assert pool._refresh_entry(original, force=True) is None
    pool._persist()
    assert "A" not in _rows(env)
    assert not fake_refresh


def test_removed_credential_metadata_does_not_resurrect(env):
    pool = _pool_from_disk(env)
    _seed(env, [_row("B")])
    pool._persist()
    assert "A" not in _rows(env)
    assert not pool.entries()


def test_cas_conflict_preserves_disk(env):
    auth.write_credential_pool(PROVIDER, [_row("A", "cas", "cas")], token_bases={"A": ("wrong", "wrong")})
    assert (_rows(env)["A"]["access_token"], _rows(env)["A"]["refresh_token"]) == ("a-A", "r-A")


def test_cas_success_and_concurrent_addition(env):
    auth.write_credential_pool(PROVIDER, [_row("A", "cas", "cas"), _row("C")], token_bases={"A": ("a-A", "r-A")})
    rows = _rows(env)
    assert set(rows) == {"A", "B", "C"}
    assert rows["A"]["access_token"] == "cas-A"
    assert rows["A"]["refresh_token"] == "cas-A"


def test_force_401_public_retry_adopts_winner(env, fake_refresh):
    winner, stale = _pool_from_disk(env), _pool_from_disk(env)
    winner.try_refresh_matching(credential_id="A")
    result = stale.try_refresh_matching(api_key_hint="a-A", credential_id="A")
    assert result is not None and result.access_token == "new-a"
    assert len(fake_refresh) == 1


def test_stale_remove_does_not_revive_peer(env):
    pool = cp.CredentialPool(PROVIDER, [cp.PooledCredential.from_dict(PROVIDER, row)
                                      for row in _rows(env).values()])
    _seed(env, [_row("A")])
    assert pool.remove_index(1).id == "A"
    assert not _rows(env)


def test_explicit_removed_id_ignores_input(env):
    auth.write_credential_pool(PROVIDER, [_row("A"), _row("B")], removed_ids=["A"])
    assert set(_rows(env)) == {"B"}


def test_refresh_failure_is_account_local(env, monkeypatch):
    before = _rows(env)["B"]
    def fail(*args):
        raise RuntimeError("synthetic invalid_grant")
    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", fail)
    pool = _pool_from_disk(env)
    assert pool._refresh_entry(pool.entries()[0], force=True) is None
    assert _rows(env)["B"] == before
    assert not json.loads(env.read_text()).get("providers", {}).get(PROVIDER)


def test_singleton_login_matches_full_prior_pair(env):
    alias, independent, singleton = _row("alias"), _row("independent"), _row("singleton")
    for row in (alias, independent, singleton):
        row["access_token"] = "old-a"
    alias["refresh_token"] = singleton["refresh_token"] = "old-r"
    singleton["source"] = "device_code"
    data = dict(credential_pool={PROVIDER: [alias, independent, singleton]},
                providers={PROVIDER: dict(tokens=dict(access_token="old-a", refresh_token="old-r"))})
    env.write_text(json.dumps(data))
    auth._save_codex_tokens(dict(access_token="login-a", refresh_token="login-r"))
    rows = _rows(env)
    assert rows["alias"]["access_token"] == rows["singleton"]["access_token"] == "login-a"
    assert rows["independent"] == independent


def _child(auth_file, home, barrier, queue, cred_id):
    import os
    os.environ.update(HOME=home, HERMES_HOME=home, HERMES_AUTH_HOME=home, XDG_CONFIG_HOME=home)
    auth._auth_file_path = lambda: Path(auth_file)
    auth._auth_lock_path = lambda: Path(home) / "auth.lock"
    auth._global_auth_file_path = lambda: None
    auth._auth_target_lock_holders.clear()
    cp.get_pool_strategy = lambda *a, **k: "round_robin"
    def fake(access, refresh):
        with open(auth_file + ".calls", "a") as f:
            f.write(refresh + "\n")
        return dict(access_token="new-" + access, refresh_token="new-" + refresh)
    auth.refresh_codex_oauth_pure = fake
    pool = _pool_from_disk(Path(auth_file), cred_id)
    original = pool.entries()[0]
    barrier.wait(timeout=10)
    result = pool._refresh_entry(original, force=True)
    queue.put((result.access_token, result.refresh_token))


@pytest.mark.skipif("fork" not in multiprocessing.get_all_start_methods(), reason="requires fork")
@pytest.mark.parametrize("accounts", [("A", "A"), ("A", "B")])
def test_two_process_fork_race(env, accounts):
    ctx = multiprocessing.get_context("fork")
    barrier, queue = ctx.Barrier(2), ctx.Queue()
    procs = [ctx.Process(target=_child, args=(str(env), str(env.parent), barrier, queue, account))
             for account in accounts]
    try:
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=15)
            assert not p.is_alive() and p.exitcode == 0
        pairs = [queue.get(timeout=5) for _ in procs]
        assert set(pairs) == {("new-a-" + a, "new-r-" + a) for a in accounts}
        calls = (env.parent / "auth.json.calls").read_text().splitlines()
        assert sorted(calls) == sorted({"r-" + a for a in accounts})
    finally:
        for p in procs:
            if p.is_alive():
                p.terminate()
            if p.pid is not None:
                p.join(timeout=5)
        queue.close()
