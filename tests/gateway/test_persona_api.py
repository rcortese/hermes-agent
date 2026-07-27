"""Tests for the target-side ask-only Persona API (A2A v1).

Covers:
- gateway/persona_api.py: config parsing/validation, deny-by-default
  caller->target->ask matrix, closed receipt envelope, fail-closed redaction.
- gateway/platforms/api_server.py POST /api/persona/ask: auth ordering
  (admin key rejected before/regardless of caller matrix), spoofed identity
  fields ignored, secret-shaped answers redacted, redaction-failure fail
  closed, and the ordinary happy path.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway import persona_api
from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter

MERCURY_TOKEN = "caller-mercury-secret-token-0123456789"
NOACCESS_TOKEN = "caller-noaccess-secret-token-0123456789"


def _inbound_config(*, mercury_token: str = MERCURY_TOKEN) -> persona_api.InboundConfig:
    callers = {
        "mercury": persona_api.CallerRule(
            caller_id="mercury",
            allow_targets=frozenset({"atlas"}),
            token=mercury_token,
        ),
        "no-access": persona_api.CallerRule(
            caller_id="no-access",
            allow_targets=frozenset(),  # deny-by-default: no targets granted
            token=NOACCESS_TOKEN,
        ),
    }
    return persona_api.InboundConfig(self_target="atlas", callers=callers)


def _make_adapter(api_key: str = "") -> APIServerAdapter:
    extra = {"key": api_key} if api_key else {}
    return APIServerAdapter(PlatformConfig(enabled=True, extra=extra))


def _make_app(adapter: APIServerAdapter) -> web.Application:
    app = web.Application()
    app["api_server_adapter"] = adapter
    app.router.add_post("/api/persona/ask", adapter._handle_persona_ask)
    return app


# ---------------------------------------------------------------------------
# gateway/persona_api.py — config parsing & matrix logic
# ---------------------------------------------------------------------------


class TestNormalizeId:
    def test_valid_id_passes_through(self):
        assert persona_api.normalize_id("atlas") == "atlas"
        assert persona_api.normalize_id("  Atlas-01  ") == "atlas-01"

    def test_invalid_ids_rejected(self):
        for bad in ("", " ", "-leading-dash", "has space", "has/slash", "a" * 65, None, 123):
            assert persona_api.normalize_id(bad) == ""


class TestLoadInboundConfig:
    def test_missing_section_returns_none(self):
        assert persona_api.load_inbound_config({}) is None

    def test_missing_self_target_returns_none(self):
        cfg = {"persona_api": {"inbound": {"callers": {"mercury": {"allow_targets": ["atlas"], "token": "x" * 20}}}}}
        assert persona_api.load_inbound_config(cfg) is None

    def test_no_callers_returns_none(self):
        cfg = {"persona_api": {"self_target": "atlas", "inbound": {"callers": {}}}}
        assert persona_api.load_inbound_config(cfg) is None

    def test_valid_config_parses(self):
        cfg = {
            "persona_api": {
                "self_target": "Atlas",
                "inbound": {
                    "callers": {
                        "mercury": {"allow_targets": ["atlas"], "token": "caller-token-0123456789"},
                    }
                },
            }
        }
        inbound = persona_api.load_inbound_config(cfg)
        assert inbound is not None
        assert inbound.self_target == "atlas"
        assert "mercury" in inbound.callers
        assert inbound.callers["mercury"].allow_targets == frozenset({"atlas"})

    def test_duplicate_credential_across_callers_fails_closed(self):
        """Two callers configured with the same token must never both
        authenticate -- the whole inbound config is treated as invalid
        (feature disabled) rather than letting one silently win."""
        shared_token = "shared-secret-token-0123456789"
        cfg = {
            "persona_api": {
                "self_target": "atlas",
                "inbound": {
                    "callers": {
                        "mercury": {"allow_targets": ["atlas"], "token": shared_token},
                        "venus": {"allow_targets": ["atlas"], "token": shared_token},
                    }
                },
            }
        }
        assert persona_api.load_inbound_config(cfg) is None

    def test_duplicate_credential_warning_never_contains_token_value(self, caplog):
        shared_token = "shared-secret-token-0123456789"
        cfg = {
            "persona_api": {
                "self_target": "atlas",
                "inbound": {
                    "callers": {
                        "mercury": {"allow_targets": ["atlas"], "token": shared_token},
                        "venus": {"allow_targets": ["atlas"], "token": shared_token},
                    }
                },
            }
        }
        with caplog.at_level("WARNING"):
            assert persona_api.load_inbound_config(cfg) is None
        full_log = "\n".join(record.getMessage() for record in caplog.records)
        assert shared_token not in full_log
        assert "mercury" in full_log
        assert "venus" in full_log

    def test_distinct_tokens_are_unaffected_by_duplicate_check(self):
        """Sanity check: non-colliding tokens across callers must still load
        and map exactly -- the duplicate check must not have false positives."""
        cfg = {
            "persona_api": {
                "self_target": "atlas",
                "inbound": {
                    "callers": {
                        "mercury": {"allow_targets": ["atlas"], "token": "mercury-token-0123456789"},
                        "venus": {"allow_targets": ["atlas"], "token": "venus-token-0123456789"},
                    }
                },
            }
        }
        inbound = persona_api.load_inbound_config(cfg)
        assert inbound is not None
        assert inbound.callers["mercury"].token == "mercury-token-0123456789"
        assert inbound.callers["venus"].token == "venus-token-0123456789"

    def test_invalid_caller_entry_is_dropped_not_fatal(self):
        cfg = {
            "persona_api": {
                "self_target": "atlas",
                "inbound": {
                    "callers": {
                        "mercury": {"allow_targets": ["atlas"], "token": "caller-token-0123456789"},
                        "bad id!": {"allow_targets": ["atlas"], "token": "x"},
                        "also-bad": "not-a-dict",
                    }
                },
            }
        }
        inbound = persona_api.load_inbound_config(cfg)
        assert inbound is not None
        assert list(inbound.callers.keys()) == ["mercury"]


class TestLoadOutboundConfig:
    def test_missing_section_returns_none(self):
        assert persona_api.load_outbound_config({}) is None

    def test_invalid_url_dropped(self):
        cfg = {"persona_api": {"outbound": {"targets": {"bob": {"url": "not-a-url", "token": "x" * 20}}}}}
        assert persona_api.load_outbound_config(cfg) is None

    def test_valid_target_parses(self):
        cfg = {"persona_api": {"outbound": {"targets": {"bob": {"url": "https://bob.example.com/", "token": "tok"}}}}}
        outbound = persona_api.load_outbound_config(cfg)
        assert outbound is not None
        assert outbound.targets["bob"].url == "https://bob.example.com"
        assert outbound.targets["bob"].token == "tok"


class TestResolveCallerAndMatrix:
    def test_resolves_matching_caller(self):
        inbound = _inbound_config()
        assert persona_api.resolve_caller(inbound, MERCURY_TOKEN) == "mercury"

    def test_unknown_token_returns_none(self):
        inbound = _inbound_config()
        assert persona_api.resolve_caller(inbound, "totally-wrong-token") is None

    def test_empty_token_never_matches(self):
        inbound = persona_api.InboundConfig(
            self_target="atlas",
            callers={"mercury": persona_api.CallerRule(caller_id="mercury", allow_targets=frozenset({"atlas"}), token="")},
        )
        assert persona_api.resolve_caller(inbound, "") is None

    def test_deny_by_default_when_no_allow_targets(self):
        inbound = _inbound_config()
        assert persona_api.is_ask_authorized(inbound, "no-access") is False

    def test_authorized_when_target_listed(self):
        inbound = _inbound_config()
        assert persona_api.is_ask_authorized(inbound, "mercury") is True

    def test_unknown_caller_denied(self):
        inbound = _inbound_config()
        assert persona_api.is_ask_authorized(inbound, "ghost") is False

    def test_distinct_tokens_map_to_exactly_one_caller_each(self):
        inbound = _inbound_config()
        assert persona_api.resolve_caller(inbound, MERCURY_TOKEN) == "mercury"
        assert persona_api.resolve_caller(inbound, NOACCESS_TOKEN) == "no-access"

    def test_duplicate_token_resolves_to_no_caller(self):
        """Defense in depth: even if a duplicate-credential InboundConfig
        somehow reaches resolve_caller (load_inbound_config already rejects
        this at load time), the presented token must authenticate as
        nobody -- never as whichever caller happens to match, e.g. the last
        one in iteration order."""
        shared_token = "shared-secret-token-0123456789"
        inbound = persona_api.InboundConfig(
            self_target="atlas",
            callers={
                "mercury": persona_api.CallerRule(
                    caller_id="mercury", allow_targets=frozenset({"atlas"}), token=shared_token,
                ),
                "venus": persona_api.CallerRule(
                    caller_id="venus", allow_targets=frozenset(), token=shared_token,
                ),
            },
        )
        assert persona_api.resolve_caller(inbound, shared_token) is None


class TestBuildReceipt:
    def test_closed_shape(self):
        receipt = persona_api.build_receipt(status="ok", target="atlas", correlation_id="abc", answer_text="hi")
        assert set(receipt.keys()) == {
            "object", "status", "target", "correlation_id", "answer_text", "error_code", "message",
        }


class TestSafeRedactFailsClosed:
    def test_normal_redaction_masks_secret(self):
        secret = "sk-persona-test-secret-1234567890"
        out = persona_api.safe_redact(f"here is a key {secret}")
        assert out is not None
        assert secret not in out

    def test_redaction_exception_returns_none(self):
        with patch("gateway.persona_api.redact_sensitive_text", side_effect=RuntimeError("boom")):
            assert persona_api.safe_redact("anything") is None

    def test_none_input_returns_none(self):
        assert persona_api.safe_redact(None) is None


# ---------------------------------------------------------------------------
# POST /api/persona/ask — HTTP-level behavior
# ---------------------------------------------------------------------------


class TestPersonaAskRoute:
    def test_not_configured_returns_404(self):
        adapter = _make_adapter()
        app = _make_app(adapter)

        async def _run():
            with patch("gateway.persona_api.load_inbound_config", return_value=None):
                async with TestClient(TestServer(app)) as cli:
                    resp = await cli.post("/api/persona/ask", json={"question": "hi"})
                    assert resp.status == 404

        asyncio.run(_run())

    def test_wrong_caller_returns_401(self):
        adapter = _make_adapter()
        app = _make_app(adapter)

        async def _run():
            with patch("gateway.persona_api.load_inbound_config", return_value=_inbound_config()):
                async with TestClient(TestServer(app)) as cli:
                    resp = await cli.post(
                        "/api/persona/ask",
                        json={"question": "hi"},
                        headers={"Authorization": "Bearer not-a-real-token"},
                    )
                    assert resp.status == 401
                    body = await resp.json()
                    assert body["error"]["code"] == "persona_invalid_credential"

        asyncio.run(_run())

    def test_admin_key_rejected_on_persona_route(self):
        """API_SERVER_KEY must never authorize this route, even standalone."""
        adapter = _make_adapter(api_key="sk-admin-secret-0123456789")
        app = _make_app(adapter)

        async def _run():
            with patch("gateway.persona_api.load_inbound_config", return_value=_inbound_config()):
                async with TestClient(TestServer(app)) as cli:
                    resp = await cli.post(
                        "/api/persona/ask",
                        json={"question": "hi"},
                        headers={"Authorization": "Bearer sk-admin-secret-0123456789"},
                    )
                    assert resp.status == 401
                    body = await resp.json()
                    assert body["error"]["code"] == "persona_admin_key_rejected"

        asyncio.run(_run())

    def test_admin_key_rejected_even_if_it_collides_with_a_caller_token(self):
        """Defense in depth: even a config accident where a caller token equals
        the admin key must still be rejected as the admin key, not treated as
        a valid caller credential."""
        admin_key = "sk-admin-secret-0123456789"
        adapter = _make_adapter(api_key=admin_key)
        app = _make_app(adapter)
        colliding_inbound = _inbound_config(mercury_token=admin_key)

        async def _run():
            with patch("gateway.persona_api.load_inbound_config", return_value=colliding_inbound):
                async with TestClient(TestServer(app)) as cli:
                    resp = await cli.post(
                        "/api/persona/ask",
                        json={"question": "hi"},
                        headers={"Authorization": f"Bearer {admin_key}"},
                    )
                    assert resp.status == 401
                    body = await resp.json()
                    assert body["error"]["code"] == "persona_admin_key_rejected"

        asyncio.run(_run())

    def test_deny_by_default_unauthorized_target(self):
        adapter = _make_adapter()
        app = _make_app(adapter)

        async def _run():
            with patch("gateway.persona_api.load_inbound_config", return_value=_inbound_config()):
                async with TestClient(TestServer(app)) as cli:
                    resp = await cli.post(
                        "/api/persona/ask",
                        json={"question": "hi"},
                        headers={"Authorization": f"Bearer {NOACCESS_TOKEN}"},
                    )
                    assert resp.status == 403
                    body = await resp.json()
                    assert body["error"]["code"] == "persona_denied"

        asyncio.run(_run())

    def test_spoofed_identity_fields_are_ignored(self):
        """Caller/target identity must come only from the credential + server
        config, never from the request body -- even when the body tries to
        claim a different (unauthorized) caller/target."""
        adapter = _make_adapter()
        app = _make_app(adapter)
        adapter._run_persona_ask = AsyncMock(return_value=("Paris.", False))

        async def _run():
            with patch("gateway.persona_api.load_inbound_config", return_value=_inbound_config()):
                async with TestClient(TestServer(app)) as cli:
                    resp = await cli.post(
                        "/api/persona/ask",
                        json={"question": "What is the capital of France?", "caller": "no-access", "target": "other-target"},
                        headers={"Authorization": f"Bearer {MERCURY_TOKEN}"},
                    )
                    assert resp.status == 200
                    body = await resp.json()
                    assert body["status"] == "ok"
                    # target is the server's OWN configured identity, not the
                    # spoofed body field.
                    assert body["target"] == "atlas"
                    assert body["answer_text"] == "Paris."

        asyncio.run(_run())

    def test_ordinary_answer(self):
        adapter = _make_adapter()
        app = _make_app(adapter)
        adapter._run_persona_ask = AsyncMock(return_value=("The answer is 4.", False))

        async def _run():
            with patch("gateway.persona_api.load_inbound_config", return_value=_inbound_config()):
                async with TestClient(TestServer(app)) as cli:
                    resp = await cli.post(
                        "/api/persona/ask",
                        json={"question": "What is 2+2?"},
                        headers={"Authorization": f"Bearer {MERCURY_TOKEN}"},
                    )
                    assert resp.status == 200
                    body = await resp.json()
                    assert body["status"] == "ok"
                    assert body["target"] == "atlas"
                    assert body["answer_text"] == "The answer is 4."
                    assert body["correlation_id"]
                    assert set(body.keys()) == {
                        "object", "status", "target", "correlation_id", "answer_text", "error_code", "message",
                    }

        asyncio.run(_run())

    def test_secret_shaped_answer_is_redacted(self):
        secret = "sk-leaked-persona-secret-1234567890"
        adapter = _make_adapter()
        app = _make_app(adapter)
        adapter._run_persona_ask = AsyncMock(return_value=(f"Here is the key: {secret}", False))

        async def _run():
            with patch("gateway.persona_api.load_inbound_config", return_value=_inbound_config()):
                async with TestClient(TestServer(app)) as cli:
                    resp = await cli.post(
                        "/api/persona/ask",
                        json={"question": "leak a secret"},
                        headers={"Authorization": f"Bearer {MERCURY_TOKEN}"},
                    )
                    assert resp.status == 200
                    body = await resp.json()
                    assert secret not in body["answer_text"]

        asyncio.run(_run())

    def test_stdout_traceback_style_error_never_leaks_raw(self):
        adapter = _make_adapter()
        app = _make_app(adapter)

        async def _boom(question):
            raise RuntimeError(
                'Traceback (most recent call last):\n  File "x.py"\nRuntimeError: '
                "OPENAI_API_KEY=sk-shouldnotleak1234567890"
            )

        adapter._run_persona_ask = _boom

        async def _run():
            with patch("gateway.persona_api.load_inbound_config", return_value=_inbound_config()):
                async with TestClient(TestServer(app)) as cli:
                    resp = await cli.post(
                        "/api/persona/ask",
                        json={"question": "hi"},
                        headers={"Authorization": f"Bearer {MERCURY_TOKEN}"},
                    )
                    assert resp.status == 502
                    raw = await resp.text()
                    assert "Traceback" not in raw
                    assert "sk-shouldnotleak1234567890" not in raw
                    body = await resp.json()
                    assert body["status"] == "error"
                    assert body["error_code"] == "ask_failed"

        asyncio.run(_run())

    def test_redaction_failure_fails_closed(self):
        adapter = _make_adapter()
        app = _make_app(adapter)
        adapter._run_persona_ask = AsyncMock(return_value=("a perfectly ordinary answer", False))

        async def _run():
            with patch("gateway.persona_api.load_inbound_config", return_value=_inbound_config()), \
                 patch("gateway.persona_api.redact_sensitive_text", side_effect=RuntimeError("redactor exploded")):
                async with TestClient(TestServer(app)) as cli:
                    resp = await cli.post(
                        "/api/persona/ask",
                        json={"question": "hi"},
                        headers={"Authorization": f"Bearer {MERCURY_TOKEN}"},
                    )
                    assert resp.status == 502
                    body = await resp.json()
                    assert body["status"] == "error"
                    assert body["error_code"] == "redaction_failed"
                    assert body["answer_text"] is None
                    raw = await resp.text()
                    assert "a perfectly ordinary answer" not in raw

        asyncio.run(_run())

    def test_duplicate_caller_credential_route_fails_closed(self):
        """If persona_api.load_inbound_config ever returned a duplicate-
        credential config (it shouldn't -- see TestLoadInboundConfig), the
        route must still refuse to let the shared token authenticate as
        either caller, and never leak the token value in the response."""
        shared_token = "shared-secret-token-0123456789"
        adapter = _make_adapter()
        app = _make_app(adapter)
        colliding_inbound = persona_api.InboundConfig(
            self_target="atlas",
            callers={
                "mercury": persona_api.CallerRule(
                    caller_id="mercury", allow_targets=frozenset({"atlas"}), token=shared_token,
                ),
                "venus": persona_api.CallerRule(
                    caller_id="venus", allow_targets=frozenset({"atlas"}), token=shared_token,
                ),
            },
        )

        async def _run():
            with patch("gateway.persona_api.load_inbound_config", return_value=colliding_inbound):
                async with TestClient(TestServer(app)) as cli:
                    resp = await cli.post(
                        "/api/persona/ask",
                        json={"question": "hi"},
                        headers={"Authorization": f"Bearer {shared_token}"},
                    )
                    assert resp.status == 401
                    raw = await resp.text()
                    assert shared_token not in raw
                    body = await resp.json()
                    assert body["error"]["code"] == "persona_invalid_credential"

        asyncio.run(_run())

    def test_missing_question_returns_400(self):
        adapter = _make_adapter()
        app = _make_app(adapter)

        async def _run():
            with patch("gateway.persona_api.load_inbound_config", return_value=_inbound_config()):
                async with TestClient(TestServer(app)) as cli:
                    resp = await cli.post(
                        "/api/persona/ask",
                        json={},
                        headers={"Authorization": f"Bearer {MERCURY_TOKEN}"},
                    )
                    assert resp.status == 400

        asyncio.run(_run())
