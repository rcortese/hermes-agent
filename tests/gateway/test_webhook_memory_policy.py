"""Webhook memory-policy regression tests.

Service-origin webhook events are machine-to-machine inputs.  They may
need an agent run, but by default they must not become durable user
memory or Honcho peers such as ``webhook:sentinel``.
"""

import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.webhook import WebhookAdapter, _INSECURE_NO_AUTH


def _make_adapter(routes) -> WebhookAdapter:
    config = PlatformConfig(
        enabled=True,
        extra={"host": "127.0.0.1", "port": 0, "routes": routes},
    )
    return WebhookAdapter(config)


def _create_app(adapter: WebhookAdapter) -> web.Application:
    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    return app


async def _post_and_capture_event(route_config: dict):
    routes = {"sentinel": {"secret": _INSECURE_NO_AUTH, **route_config}}
    adapter = _make_adapter(routes)
    captured = []

    async def _capture(event):
        captured.append(event)

    adapter.handle_message = _capture
    app = _create_app(adapter)

    body = json.dumps({"alert": {"name": "MossDown"}}).encode()
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post(
            "/webhooks/sentinel",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Delivery": "delivery-1",
            },
        )
        assert resp.status == 202

    await asyncio.sleep(0.05)
    assert len(captured) == 1
    return captured[0]


@pytest.mark.asyncio
async def test_agent_dispatch_webhook_defaults_to_skip_memory():
    event = await _post_and_capture_event(
        {
            "prompt": "Alert: {alert.name}",
            "deliver": "log",
        }
    )

    assert event.source.platform.value == "webhook"
    assert event.source.user_id == "webhook:sentinel"
    assert event.memory_policy == "skip"


@pytest.mark.asyncio
async def test_explicit_skip_memory_policy_is_preserved():
    event = await _post_and_capture_event(
        {
            "prompt": "Alert: {alert.name}",
            "deliver": "log",
            "memory_policy": "skip",
        }
    )

    assert event.memory_policy == "skip"
    assert event.source.user_id == "webhook:sentinel"


@pytest.mark.asyncio
async def test_trusted_human_policy_without_mapping_fails_closed_to_skip():
    event = await _post_and_capture_event(
        {
            "prompt": "Operator note: {alert.name}",
            "deliver": "log",
            "memory_policy": "human",
        }
    )

    assert event.memory_policy == "skip"
    assert event.source.user_id == "webhook:sentinel"
