"""Causal guards for the core-owned five-API A2A surface."""

from __future__ import annotations

import pytest

from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest


PUBLIC_A2A_TOOLS = (
    "a2a_call",
    "a2a_discover",
    "a2a_list",
    "a2a_history",
    "a2a_orchestrate",
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


def test_plugin_context_rejects_every_reserved_a2a_tool(tmp_path):
    from hermes_cli.plugins import ReservedCoreA2ANameError

    ctx = _context(tmp_path)
    for name in PUBLIC_A2A_TOOLS:
        with pytest.raises(ReservedCoreA2ANameError, match=name):
            ctx.register_tool(
                name=name,
                toolset="ordinary",
                schema={},
                handler=lambda _: "unexpected",
                override=True,
            )


def test_discovery_rejects_a_reserved_a2a_tool_before_plugin_execution(monkeypatch):
    from hermes_cli.plugins import ReservedCoreA2ANameError

    manager = PluginManager()
    executed = []
    manifest = _manifest(provides_tools=["a2a_orchestrate"])
    monkeypatch.setattr(manager, "_scan_directory", lambda *args, **kwargs: [manifest])
    monkeypatch.setattr(manager, "_scan_entry_points", lambda: [])
    monkeypatch.setattr(manager, "_load_plugin", lambda candidate: executed.append(candidate))

    with pytest.raises(ReservedCoreA2ANameError, match="a2a_orchestrate"):
        manager._discover_and_load_inner()
    assert executed == []


def test_core_registration_preserves_exactly_five_public_a2a_apis():
    from gateway.platform_registry import platform_registry
    from hermes_cli.plugins import register_core_a2a_builtin
    from tools.registry import registry

    for name in PUBLIC_A2A_TOOLS:
        registry._tools.pop(name, None)
    platform_registry.unregister("a2a")
    register_core_a2a_builtin()

    platform = platform_registry.get("a2a")
    assert platform is not None and platform.source == "builtin"
    registered = {
        name for name in PUBLIC_A2A_TOOLS if registry.get_entry(name) is not None
    }
    assert registered == set(PUBLIC_A2A_TOOLS)


def test_a2a_package_retains_default_port_9900():
    from plugins.platforms.a2a.adapter import _DEFAULT_PORT

    assert _DEFAULT_PORT == 9900
