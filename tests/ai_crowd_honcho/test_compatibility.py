"""Offline candidate-byte compatibility probes; all identities and credentials synthetic."""
import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from agent import moss_memory_gate as gate
from agent.agent_init import _should_skip_memory_for_runtime
from plugins.memory.honcho import HonchoMemoryProvider
from plugins.memory.honcho import client as hc


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setenv('AGENT_NAME', 'moss')
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    for name in ('HERMES_SAFE_MODE', 'HERMES_IGNORE_RULES', 'HERMES_IGNORE_USER_CONFIG'):
        monkeypatch.delenv(name, raising=False)
    policy = {'version': 1, 'workspace': 'moss-rodolfo', 'base_url': 'http://honcho-api:8000',
              'profiles': ['default', 'moss'], 'homes': [str(tmp_path)], 'telegram_owner_id': '12345'}
    monkeypatch.setattr(gate, 'policy', lambda: policy)
    monkeypatch.setattr('hermes_constants.get_hermes_home', lambda: tmp_path)
    monkeypatch.setattr(gate, '_key', lambda: b'x' * 32)
    token = gate.api_admission.set(None)
    gate._seen.clear()
    yield policy
    gate.api_admission.reset(token)


def test_numeric_budget_preserved_but_credentials_scrubbed():
    assert gate.scrub('token_budget=1800') == 'token_budget=1800'
    for value in ('token=1800', 'access_token=1800', 'token_budget=opaque-secret',
                  'api_key=sk-' + 'A' * 48, 'token_budget=sk-' + 'B' * 48):
        assert gate.scrub(value) == '[content omitted: credential detected]'
    with gate.safe_http_client('http://honcho-api:8000', 5) as client:
        hook = client.event_hooks['request'][0]
        hook(httpx.Request('GET', 'http://honcho-api:8000/v3/context?token_budget=1800'))
        with pytest.raises(ValueError):
            hook(httpx.Request('GET', 'http://honcho-api:8000/v3/context?token_budget=opaque-secret'))


@pytest.mark.parametrize('persona', ['roy', 'jen', 'richmond', 'denholm', 'the-elders', ''])
def test_non_moss_preserves_original_paths(monkeypatch, persona):
    monkeypatch.setenv('AGENT_NAME', persona)
    monkeypatch.setattr(gate, 'policy', Mock(side_effect=AssertionError('must not read Moss policy')))
    assert not gate.moss_runtime()
    assert gate.read_only_status(SimpleNamespace()) is False
    assert _should_skip_memory_for_runtime(platform='api_server')
    assert not _should_skip_memory_for_runtime(platform='telegram')
    cfg = SimpleNamespace(resolve_session_name=lambda **kw: 'original-key')
    assert HonchoMemoryProvider()._resolve_session_key(cfg, 's') == 'original-key'


def test_init_admission_and_explicit_skip():
    assert _should_skip_memory_for_runtime(platform='api_server', session_id='s')
    with gate.admission_scope({'principal': 'Rodolfo', 'session_id': 's', 'profile': 'default'}):
        assert not _should_skip_memory_for_runtime(platform='api_server', session_id='s')
        assert _should_skip_memory_for_runtime(platform='api_server', session_id='wrong')
        assert _should_skip_memory_for_runtime(platform='api_server', session_id='s', explicit_skip_memory=True)
        assert _should_skip_memory_for_runtime(platform='cron', session_id='s')


def test_missing_policy_denies_before_any_client_or_provider_setup(monkeypatch):
    monkeypatch.setattr(gate, 'policy', lambda: None)
    monkeypatch.setattr(hc.HonchoClientConfig, 'from_global_config', Mock(side_effect=AssertionError('no config read')))
    provider = HonchoMemoryProvider()
    provider.initialize('s', platform='telegram', agent_context='primary', user_id='12345', chat_id='12345', chat_type='dm')
    assert provider._cron_skipped
    with pytest.raises(ValueError, match='mismatch'):
        hc.get_honcho_client(hc.HonchoClientConfig(workspace_id='moss-rodolfo', base_url='http://honcho-api:8000', api_key='local'))


def test_moss_cached_client_never_refreshes_oauth(monkeypatch):
    cached = object()
    monkeypatch.setattr(hc, '_slot_for', lambda _: SimpleNamespace(peek=lambda: cached))
    monkeypatch.setattr(hc, '_refresh_oauth', Mock(side_effect=AssertionError('Moss must not refresh OAuth')))
    cfg = hc.HonchoClientConfig(workspace_id='moss-rodolfo', base_url='http://honcho-api:8000', api_key='local')
    assert hc.get_honcho_client(cfg) is cached


def test_moss_fresh_client_never_refreshes_oauth(monkeypatch):
    monkeypatch.setattr(hc, '_slot_for', lambda _: SimpleNamespace(peek=lambda: None, get=lambda fn: 'factory-deferred'))
    monkeypatch.setattr(hc, '_refresh_oauth', Mock(side_effect=AssertionError('Moss must not read OAuth')))
    cfg = hc.HonchoClientConfig(workspace_id='moss-rodolfo', base_url='http://honcho-api:8000', api_key='local')
    assert hc.get_honcho_client(cfg) == 'factory-deferred'


@pytest.mark.parametrize('api_key', [None, '', 'local'])
def test_moss_factory_uses_guarded_transport_for_local_placeholder(monkeypatch, api_key):
    import sys
    monkeypatch.setattr(hc, '_slot_for', lambda _: SimpleNamespace(peek=lambda: None, get=lambda fn: fn()))
    monkeypatch.setattr(hc, '_refresh_oauth', Mock(side_effect=AssertionError('no OAuth')))
    monkeypatch.setitem(sys.modules, 'honcho', SimpleNamespace(Honcho=lambda **kw: kw))
    monkeypatch.setattr('tools.lazy_deps.ensure', lambda *a, **kw: None)
    cfg = hc.HonchoClientConfig(workspace_id='moss-rodolfo', base_url='http://honcho-api:8000', api_key=api_key, timeout=5)
    result = hc.get_honcho_client(cfg)
    assert isinstance(result, dict)  # synthetic SDK constructor returns its kwargs
    assert result['api_key'] == 'local'
    with result['http_client'] as client:
        assert client.event_hooks['request']
        assert not client.follow_redirects


def test_non_moss_retains_oauth_refresh(monkeypatch):
    monkeypatch.setenv('AGENT_NAME', 'roy')
    cached = object()
    slot = SimpleNamespace(peek=lambda: cached)
    monkeypatch.setattr(hc, '_slot_for', lambda _: slot)
    refresh = Mock()
    monkeypatch.setattr(hc, '_refresh_oauth', refresh)
    cfg = hc.HonchoClientConfig()
    assert hc.get_honcho_client(cfg) is cached
    refresh.assert_called_once_with(cfg, cached, slot)


def test_read_only_status_no_sdk_creation(monkeypatch, capsys):
    requests = []
    transport = httpx.MockTransport(lambda req: (requests.append(req) or httpx.Response(200, json={'total': 1, 'items': [{'id': 'moss-rodolfo'}]})))
    monkeypatch.setattr(gate, 'safe_http_client', lambda *a, **kw: httpx.Client(transport=transport))
    cfg = SimpleNamespace(workspace_id='moss-rodolfo', base_url='http://honcho-api:8000')
    assert gate.read_only_status(cfg)
    assert [r.url.path for r in requests] == ['/v3/workspaces/list']
    assert 'NOT VERIFIED' in capsys.readouterr().out


def test_middleware_real_signed_request_and_reset(monkeypatch):
    from gateway.platforms.api_server import APIServerAdapter
    import time
    adapter = object.__new__(APIServerAdapter)
    monkeypatch.setattr(adapter, '_derive_browser_control_principal', lambda _: 'synthetic')
    monkeypatch.setattr(adapter, '_browser_control_transport_family', lambda _: 'synthetic')
    monkeypatch.setattr(adapter, '_resolve_request_profile', lambda _: None)
    monkeypatch.setattr(adapter, '_profile_scope', lambda _: nullcontext())
    body = b'{"session_id":"s"}'
    headers = gate.sign_request({'profile': 'default', 'expires': time.time()+60}, body, 's', 'default')
    headers.update({'X-Hermes-Session-Id': 's', 'X-Hermes-Session-Key': 'webui:s'})
    async def read(): return body
    request = SimpleNamespace(method='POST', path='/v1/runs', headers=headers, read=read)
    async def handler(_):
        admission = gate.api_admission.get()
        assert admission is not None and admission['session_id'] == 's'
        raise RuntimeError('synthetic handler failure')
    async def run():
        with pytest.raises(RuntimeError, match='synthetic handler'):
            await adapter._make_profile_prefix_middleware()(request, handler)
        assert gate.api_admission.get() is None
    asyncio.run(run())


def test_task_and_executor_context_isolation():
    # Adapted from Claude creation 5cf1dbd4: explicit scope matches Runs workers.
    async def run():
        loop = asyncio.get_running_loop()
        ready = asyncio.Event()
        seen = []
        def worker(admission):
            assert gate.api_admission.get() is None
            try:
                with gate.admission_scope(admission):
                    assert gate.api_admission.get() == admission
                    raise RuntimeError('synthetic')
            except RuntimeError:
                pass
            assert gate.api_admission.get() is None
            return admission
        async def task(admission):
            with gate.admission_scope(admission):
                seen.append(admission)
                if len(seen) == 2: ready.set()
                await ready.wait()
                assert gate.api_admission.get() == admission
                return await loop.run_in_executor(None, worker, gate.api_admission.get())
        a, b = {'session_id': 'a'}, {'session_id': 'b'}
        assert await asyncio.gather(asyncio.create_task(task(a)), asyncio.create_task(task(b))) == [a, b]
        assert gate.api_admission.get() is None
    asyncio.run(run())
