"""Offline regression for configured search chains across the upstream memo layer."""
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from tools import web_tools as web
from tools import web_tools_fallback as chain
from agent import web_search_registry as registry
from tools.web_result_cache import search_memo

@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    search_memo.clear()
    monkeypatch.setattr(web, '_load_web_config', lambda: {'search_fallback_backends': ['brave-free']})
    monkeypatch.setattr(web, '_is_backend_available', lambda name: True)
    yield
    search_memo.clear()

def provider(name, result):
    search = Mock(side_effect=result) if isinstance(result, Exception) else Mock(return_value=result)
    return SimpleNamespace(name=name, supports_search=lambda: True, search=search)

@pytest.mark.parametrize('failure', [RuntimeError('offline failure'), {'success': False, 'error': 'fixture'}, 'invalid'])
def test_ordered_failure_and_cache_identity(monkeypatch, failure):
    primary = provider('firecrawl', failure)
    fallback = provider('brave-free', {'success': True, 'data': {'web': [{'title': 'fixture'}]}})
    monkeypatch.setattr(registry, 'get_provider', lambda name: fallback)
    monkeypatch.setattr(web, '_rescue_search', Mock(side_effect=AssertionError('implicit rescue forbidden')))
    result = web._memoized_search(primary, 'offline fixture', 1)
    assert result['success'] is True
    assert result['metadata']['backend'] == 'brave-free'
    assert result['metadata']['fallback_from'] == 'firecrawl'
    assert search_memo.lookup('firecrawl', 'offline fixture', 1) is None
    assert primary.search.call_count == fallback.search.call_count == 1

def test_custom_registry_provider_and_order(monkeypatch):
    monkeypatch.setattr(web, '_load_web_config', lambda: {'search_fallback_backends': ['custom', 'custom', 'firecrawl']})
    custom = provider('custom', {'success': True, 'data': {'web': []}})
    monkeypatch.setattr(registry, 'get_provider', lambda name: custom if name == 'custom' else None)
    monkeypatch.setattr(web, '_registered_web_provider', lambda name: custom if name == 'custom' else None)
    assert chain._get_search_fallback_backends('firecrawl') == ['custom']
    assert chain.ordered_search(provider('firecrawl', RuntimeError('fixture')), 'firecrawl', 'offline', 1)['success']


@pytest.mark.parametrize('configured', [
    ['firecrawl'], ['missing-custom'], ['firecrawl', 'missing-custom'],
    [], None, '', 123,
])
@pytest.mark.parametrize('failure', [RuntimeError('offline'), {'success': False, 'error': 'offline'}])
def test_explicit_primary_only_policy_never_rescues(monkeypatch, configured, failure):
    monkeypatch.setattr(web, '_load_web_config', lambda: {'search_fallback_backends': configured})
    monkeypatch.setattr(web, '_registered_web_provider', lambda name: None)
    monkeypatch.setattr(registry, 'get_provider', Mock(side_effect=AssertionError('no alternatives')))
    rescue = Mock(return_value={'success': True, 'data': {'web': []}})
    monkeypatch.setattr(web, '_rescue_eligible', lambda primary: True)
    monkeypatch.setattr(web, '_rescue_search', rescue)
    primary = provider('firecrawl', failure)
    result = web._memoized_search(primary, 'explicit-only offline', 1)
    assert result['success'] is False
    rescue.assert_not_called()
    primary.search.assert_called_once()
    assert search_memo.lookup('firecrawl', 'explicit-only offline', 1) is None


def test_absent_policy_preserves_legacy_rescue(monkeypatch):
    monkeypatch.setattr(web, '_load_web_config', lambda: {})
    monkeypatch.setattr(web, '_rescue_eligible', lambda primary: True)
    rescue = Mock(return_value={'success': True, 'data': {'web': []}})
    monkeypatch.setattr(web, '_rescue_search', rescue)
    result = web._memoized_search(provider('firecrawl', RuntimeError('offline')), 'legacy offline', 1)
    assert result['success'] is True
    rescue.assert_called_once()
    assert search_memo.lookup('firecrawl', 'legacy offline', 1) is None


@pytest.mark.parametrize('metadata', ['absent', None, 'unexpected', [], 123, {}])
def test_primary_optional_metadata_success_is_memoized(monkeypatch, metadata):
    response = {'success': True, 'data': {'web': [{'title': 'offline'}]}}
    if metadata != 'absent':
        response['metadata'] = metadata
    primary = provider('firecrawl', response)
    fallback = provider('brave-free', RuntimeError('must not run'))
    monkeypatch.setattr(registry, 'get_provider', lambda name: fallback)
    monkeypatch.setattr(web, '_rescue_search', Mock(side_effect=AssertionError('must not rescue')))
    first = web._memoized_search(primary, 'metadata offline', 1)
    second = web._memoized_search(primary, 'metadata offline', 1)
    assert first == second == response
    primary.search.assert_called_once()
    fallback.search.assert_not_called()
    assert search_memo.lookup('firecrawl', 'metadata offline', 1) == response
