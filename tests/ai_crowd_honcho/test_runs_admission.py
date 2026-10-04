"""Real Runs handler/middleware with fake model; no live auth or Honcho."""
import asyncio
import json
import threading
import time
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from agent import moss_memory_gate as gate
from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from plugins.memory.honcho import HonchoMemoryProvider
from plugins.memory.honcho.client import HonchoClientConfig


def test_signed_and_unsigned_runs_keep_distinct_admissions(monkeypatch, tmp_path):
    asyncio.run(_signed_and_unsigned_runs(monkeypatch, tmp_path))


async def _signed_and_unsigned_runs(monkeypatch, tmp_path):
    monkeypatch.setenv('AGENT_NAME', 'moss')
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(gate, 'policy', lambda: {'profiles': ['default'], 'homes': [str(tmp_path)]})
    monkeypatch.setattr(gate, '_key', lambda: b'x' * 32)
    gate._seen.clear()
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={'key': 'synthetic-api-key'}))
    monkeypatch.setattr(adapter, '_profile_scope', lambda _: nullcontext())
    barrier = threading.Barrier(2, timeout=5)
    observations = {}

    def create(**kwargs):
        sid = kwargs['session_id']
        observations[sid] = {'create': gate.api_admission.get()}
        agent = MagicMock()
        agent.session_prompt_tokens = agent.session_completion_tokens = agent.session_total_tokens = 0
        def conversation(**kw):
            barrier.wait()
            observations[sid]['worker'] = gate.api_admission.get()
            return {'final_response': 'synthetic done'}
        agent.run_conversation.side_effect = conversation
        return agent
    monkeypatch.setattr(adapter, '_create_agent', create)
    app = web.Application(middlewares=[adapter._make_profile_prefix_middleware()])
    app['api_server_adapter'] = adapter
    app.router.add_post('/v1/runs', adapter._handle_runs)
    with gate.admission_scope(None):
        async with TestClient(TestServer(app)) as client:
            async def post(sid, signed):
                body = json.dumps({'session_id': sid, 'input': 'synthetic message'}).encode()
                headers = {'Authorization': 'Bearer synthetic-api-key', 'Content-Type': 'application/json', 'X-Hermes-Session-Id': sid, 'X-Hermes-Session-Key': 'webui:' + sid}
                if signed:
                    headers.update(gate.sign_request({'profile': 'default', 'expires': time.time()+60}, body, sid, 'default'))
                response = await client.post('/v1/runs', data=body, headers=headers)
                assert response.status == 202
                return (await response.json())['run_id']
            ids = await asyncio.gather(post('signed', True), post('unsigned', False))
            await asyncio.wait_for(asyncio.gather(*tuple(adapter._background_tasks)), 10)
            assert all(adapter._run_statuses[i]['status'] == 'completed' for i in ids)
        assert gate.api_admission.get() is None
    assert observations['unsigned'] == {'create': None, 'worker': None}
    assert observations['signed']['create']['session_id'] == 'signed'
    assert observations['signed']['worker'] == observations['signed']['create']


def test_memory_guidance_is_moss_only(monkeypatch):
    provider = HonchoMemoryProvider()
    provider._config = HonchoClientConfig()
    provider._recall_mode = 'tools'
    monkeypatch.setenv('AGENT_NAME', 'moss')
    text = provider.system_prompt_block()
    assert 'do not substitute a Markdown file' in text
    assert 'only after the tool reports success' in text
    assert 'Connectivity does not prove saving or recall' in text
    monkeypatch.setenv('AGENT_NAME', 'roy')
    assert 'Rodolfo' not in provider.system_prompt_block()
