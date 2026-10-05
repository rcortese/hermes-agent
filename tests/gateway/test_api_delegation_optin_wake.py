"""Operator opt-in: exercise the real delivery wrapper and in-process turn seam, offline."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from gateway.run_notifications import GatewayNotificationsMixin
from gateway.wake import WakeNotAccepted
from hermes_state import SessionDB


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_API_DELEGATION_WAKE', '1')
    db = SessionDB(tmp_path / 'state.db')
    db.create_session('parent', source='api_server', model='test')
    runner = GatewayNotificationsMixin()
    runner.config = SimpleNamespace(multiplex_profiles=False)
    runner._primary_profile_name = 'moss'
    adapter = SimpleNamespace(
        _ensure_session_db=lambda: db, _draining_response=lambda: None,
        _inflight_agent_runs=0, _active_run_tasks={}, _active_run_agents={},
        _stopping_run_ids=set(), _run_approval_registry={},
        run_internal_session_turn=AsyncMock(),
    )
    event = dict(type='async_delegation', delegation_id='deleg_test', parent_session_id='parent',
                 origin_session_id='parent', session_key='parent', status='completed')
    yield runner, adapter, event, db
    db.close()


def test_optin_persists_and_wakes_owner(setup):
    runner, adapter, event, db = setup
    assert asyncio.run(runner._self_post_api_server(adapter, 'worker result', 'parent', event))
    call = adapter.run_internal_session_turn.call_args.kwargs
    assert call['profile'] == 'moss'
    assert call['session_id'] == 'parent'
    assert 'not a new human message or approval' in call['text']
    assert 'worker result' in call['text']
    assert any(m.get('display_kind') == 'async_delegation_complete' for m in db.get_messages('parent'))


@pytest.mark.parametrize('field,value', [
    ('_inflight_agent_runs', 1), ('_active_run_tasks', {'run': object()}),
    ('_active_run_agents', {'run': object()}), ('_stopping_run_ids', {'run'}),
    ('_run_approval_registry', {'run': object()}), ('_draining_response', lambda: object()),
])
def test_busy_defers_without_persist_or_budget_consumption(setup, field, value):
    runner, adapter, event, db = setup
    setattr(adapter, field, value)
    with pytest.raises(WakeNotAccepted):
        asyncio.run(runner._self_post_api_server(adapter, 'result', 'parent', event))
    adapter.run_internal_session_turn.assert_not_called()
    assert db.get_messages('parent') == []


def test_busy_then_idle_and_duplicate(setup):
    runner, adapter, event, db = setup
    async def scenario():
        adapter._inflight_agent_runs = 1
        with pytest.raises(WakeNotAccepted):
            await runner._self_post_api_server(adapter, 'result', 'parent', event)
        adapter._inflight_agent_runs = 0
        assert await runner._self_post_api_server(adapter, 'result', 'parent', event)
        assert await runner._self_post_api_server(adapter, 'result', 'parent', event)
    asyncio.run(scenario())
    adapter.run_internal_session_turn.assert_awaited_once()
    assert len(db.get_messages('parent')) == 1


def test_failure_after_entry_does_not_repeat_effects(setup):
    runner, adapter, event, db = setup
    adapter.run_internal_session_turn.side_effect = RuntimeError('ambiguous executor failure')
    async def scenario():
        assert await runner._self_post_api_server(adapter, 'result', 'parent', event)
        assert await runner._self_post_api_server(adapter, 'result', 'parent', event)
    asyncio.run(scenario())
    adapter.run_internal_session_turn.assert_awaited_once()
    assert len(db.get_messages('parent')) == 1


@pytest.mark.parametrize('reason', ['unset', 'multiplex', 'interim', 'legacy'])
def test_default_contract_remains_persist_only(setup, monkeypatch, reason):
    runner, adapter, event, db = setup
    if reason == 'unset':
        monkeypatch.delenv('HERMES_API_DELEGATION_WAKE')
    elif reason == 'multiplex':
        runner.config.multiplex_profiles = True
    elif reason == 'interim':
        event['task_failure_notice'] = True
    else:
        event.pop('delegation_id')
    result = asyncio.run(runner._self_post_api_server(adapter, 'result', 'parent', event))
    adapter.run_internal_session_turn.assert_not_called()
    if reason == 'legacy':
        # Upstream persistence requires the stable ID too; preserve its refusal.
        assert result is False
        assert db.get_messages('parent') == []
    else:
        assert result is True
        assert len(db.get_messages('parent')) == 1


@pytest.mark.parametrize('reason', ['owner', 'helper'])
def test_missing_prerequisite_defers(setup, reason):
    runner, adapter, event, db = setup
    if reason == 'owner':
        runner._primary_profile_name = ''
    else:
        adapter.run_internal_session_turn = None
    with pytest.raises(WakeNotAccepted):
        asyncio.run(runner._self_post_api_server(adapter, 'result', 'parent', event))
    assert db.get_messages('parent') == []


def test_wrapper_refunds_busy_claim_and_completes_after_idle(setup):
    runner, adapter, event, db = setup
    runner._completion_delivery_identity = lambda evt: None
    runner._completion_event_scope = lambda evt: __import__('contextlib').nullcontext()
    runner._preflight_completion_delivery = AsyncMock(return_value=SimpleNamespace(
        proceed=True, early_result=None, claim_id='claim', delegation_id='deleg_test'))
    settled = []
    runner._settle_durable_claim = lambda op, *args: settled.append(op)
    async def inject(text, evt, **kwargs):
        return await runner._self_post_api_server(adapter, text, 'parent', evt)
    runner._inject_watch_notification = inject
    async def scenario():
        adapter._inflight_agent_runs = 1
        assert await runner._deliver_completion_notification('result', event) is False
        adapter._inflight_agent_runs = 0
        assert await runner._deliver_completion_notification('result', event) is True
    asyncio.run(scenario())
    assert settled == ['defer', 'complete']
    adapter.run_internal_session_turn.assert_awaited_once()
