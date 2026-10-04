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
