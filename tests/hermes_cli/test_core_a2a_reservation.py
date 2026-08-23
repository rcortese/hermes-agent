"""Causal guards for the restricted, core-owned A2A/9900 surface."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, cast

import pytest

from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest


PUBLIC_A2A_TOOLS = ("a2a_call",)
FORBIDDEN_A2A_TOOLS = (
    "a2a_discover",
    "a2a_list",
    "a2a_history",
    "a2a_orchestrate",
    "a2a_fanout",
)


def _manifest(**overrides) -> PluginManifest:
    values = {
        "name": "ordinary-plugin",
        "key": "ordinary-plugin",
        "source": "user",
        "kind": "standalone",
    }
    values.update(overrides)
    return PluginManifest(**values)  # type: ignore[arg-type]


def _context(tmp_path) -> PluginContext:
    return PluginContext(_manifest(), PluginManager())


def test_plugin_context_rejects_the_reserved_a2a_call_name(tmp_path):
    from hermes_cli.plugins import ReservedCoreA2ANameError

    with pytest.raises(ReservedCoreA2ANameError, match="a2a_call"):
        _context(tmp_path).register_tool(
            name="a2a_call",
            toolset="ordinary",
            schema={},
            handler=lambda _: "unexpected",
            override=True,
        )


@pytest.mark.parametrize("name", FORBIDDEN_A2A_TOOLS)
def test_plugin_context_does_not_reserve_nonexistent_a2a_apis(tmp_path, name):
    """The restricted core owns only a2a_call; removed APIs cannot be revived."""
    _context(tmp_path).register_tool(
        name=name,
        toolset="ordinary",
        schema={},
        handler=lambda _: "ordinary plugin tool",
    )


def test_discovery_rejects_the_only_reserved_a2a_tool_before_plugin_execution(monkeypatch):
    from hermes_cli.plugins import ReservedCoreA2ANameError

    manager = PluginManager()
    executed = []
    manifest = _manifest(provides_tools=["a2a_call"])
    monkeypatch.setattr(manager, "_scan_directory", lambda *args, **kwargs: [manifest])
    monkeypatch.setattr(manager, "_scan_entry_points", lambda: [])
    monkeypatch.setattr(manager, "_load_plugin", lambda candidate: executed.append(candidate))

    with pytest.raises(ReservedCoreA2ANameError, match="a2a_call"):
        manager._discover_and_load_inner()
    assert executed == []


def test_core_registration_exposes_only_a2a_call():
    from gateway.platform_registry import platform_registry
    from hermes_cli.plugins import register_core_a2a_builtin
    from tools.registry import registry

    for name in (*PUBLIC_A2A_TOOLS, *FORBIDDEN_A2A_TOOLS):
        registry._tools.pop(name, None)
    platform_registry.unregister("a2a")
    register_core_a2a_builtin()

    platform = platform_registry.get("a2a")
    assert platform is not None and platform.source == "builtin"
    assert {name for name in (*PUBLIC_A2A_TOOLS, *FORBIDDEN_A2A_TOOLS) if registry.get_entry(name)} == {
        "a2a_call"
    }


def test_restricted_client_schema_has_no_peer_or_url_control():
    from plugins.platforms.a2a import tools

    assert set(tools._SCHEMAS) == {"a2a_call"}
    schema = tools._SCHEMAS["a2a_call"].get("function")
    assert schema is not None
    params = schema.get("parameters")
    assert params is not None
    assert set(params["properties"]) == {"message", "context_id"}
    assert params["additionalProperties"] is False
    assert tools._DENHOLM_ALIAS == "denholm"
    assert tools._DENHOLM_ENDPOINT == "http://denholm:9900"


def test_restricted_client_rejects_caller_selected_endpoint():
    from plugins.platforms.a2a import tools

    assert "caller-selected endpoint" in tools.a2a_call({"message": "hi", "url": "http://elsewhere"})
    assert "caller-selected endpoint" in tools.a2a_call({"message": "hi", "agent": "http://elsewhere"})


def test_restricted_client_posts_only_to_fixed_endpoint_with_configured_bearer(monkeypatch):
    from plugins.platforms.a2a import tools

    seen = {}

    class Response:
        def read(self):
            return json.dumps({"result": {"parts": [{"text": "ack"}]}}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def open_fixed(request, *, timeout):
        seen.update(url=request.full_url, auth=request.get_header("Authorization"), timeout=timeout)
        return Response()

    monkeypatch.setattr(
        tools,
        "_load_config",
        lambda: {"a2a_agents": {"denholm": {"auth": {"type": "bearer", "token": "config-ref"}}}},
    )
    monkeypatch.setattr(tools._NO_REDIRECT_OPENER, "open", open_fixed)

    assert "ack" in tools.a2a_call({"message": "hi"})
    assert seen["url"] == "http://denholm:9900"
    assert seen["auth"] == "Bearer config-ref"


def test_redirect_handler_rejects_redirects_instead_of_retargeting_call():
    from plugins.platforms.a2a import tools

    request = urllib.request.Request("http://denholm:9900", method="POST")
    with pytest.raises(urllib.error.HTTPError, match="redirects are not permitted"):
        tools._RejectRedirects().redirect_request(
            request, cast(Any, None), 302, "Found", cast(Any, {}), "http://elsewhere"
        )


def test_a2a_package_retains_default_port_9900():
    from plugins.platforms.a2a.adapter import _DEFAULT_PORT

    assert _DEFAULT_PORT == 9900
