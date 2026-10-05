"""A config-owned category must reject, rather than hide, model overrides."""
import pytest


def test_category_hidden_override_reaches_rejection(monkeypatch):
    from tools import delegate_tool
    from tools.delegation_categories import CATEGORY_LIMITS, validate_category_routes
    task = {'goal': 'x', 'category': 'simples', 'acp_command': 'untrusted'}
    cfg = {'categories': {name: {'model': 'dummy', 'provider': 'dummy', 'reasoning_effort': 'low', 'allowed_models': ['dummy'], 'fallback_providers': []} for name in CATEGORY_LIMITS}}
    monkeypatch.setattr(delegate_tool, '_load_config', lambda: cfg)
    assert delegate_tool._strip_model_hidden_task_fields([task]) == [task]
    with pytest.raises(ValueError, match='forbidden overrides'):
        validate_category_routes(cfg, [task])
