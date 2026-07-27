"""Tests for the caller-side ``persona_rpc`` tool (ask-only Persona API / A2A v1).

Covers: closed tool schema (no url/provider/model/caller/header/credential
args), fixed target routing from server-side config (tool-call args cannot
override it), closed typed-receipt result shape, no raw exception/traceback
leakage on network or remote-side failures, fail-closed redaction on both
the remote answer and the local caller side, the bounded raw-byte read
(never ``resp.json()``) enforced before any JSON decoding, strict closed-
envelope schema validation (exact key set / types / max lengths, including
the discarded ``correlation_id``), and that an invalid raw caller-supplied
target is never echoed back into a result or audit log.
"""

import asyncio
import json
import re
from unittest.mock import AsyncMock, MagicMock, patch

from gateway import persona_api
from tools import persona_rpc
from tools.persona_rpc import PERSONA_RPC_SCHEMA, _handle_persona_rpc

_LOCAL_CORRELATION_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _outbound_config(url: str = "https://bob.example.com", token: str = "target-bob-token-0123456789"):
    return persona_api.OutboundConfig(
        targets={
            "bob": persona_api.RemoteTarget(target_id="bob", url=url, token=token),
        }
    )


def _ok_envelope(*, target="bob", correlation_id="corr-1", answer_text="42", **overrides):
    """Build a full, schema-compliant closed ask-receipt envelope.

    Starts from a genuinely-shaped success receipt (matching
    ``gateway.persona_api.build_receipt`` / the target-side handler in
    ``gateway/platforms/api_server.py``'s success path, which always leaves
    ``error_code``/``message`` unset) and lets individual tests mutate/drop/
    add keys via ``overrides`` to probe the strict schema validation.
    """
    envelope = {
        "object": "hermes.persona.ask_receipt",
        "status": "ok",
        "target": target,
        "correlation_id": correlation_id,
        "answer_text": answer_text,
        "error_code": None,
        "message": None,
    }
    envelope.update(overrides)
    return envelope


def _json_bytes(obj) -> bytes:
    return json.dumps(obj).encode("utf-8")


def _fake_session(status: int, body: bytes):
    """Build a mock aiohttp.ClientSession whose .post() returns *status* with
    *body* as the raw response bytes, read via a cursor-tracking
    ``resp.content.read(n)`` -- mirrors the production cumulative
    read-until-EOF loop (each call returns up to *n* bytes starting from
    where the last call left off, then an empty chunk at EOF, just like a
    real stream). ``resp.json()`` is deliberately not mocked/used anywhere:
    production code must never call it directly."""
    resp = AsyncMock()
    resp.status = status
    pos = 0

    async def _read(n):
        nonlocal pos
        chunk = body[pos:pos + n]
        pos += len(chunk)
        return chunk

    resp.content = MagicMock()
    resp.content.read = AsyncMock(side_effect=_read)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)

    session = AsyncMock()
    session.post = MagicMock(return_value=resp)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session, resp


def _fake_session_chunks(status: int, chunks):
    """Like ``_fake_session``, but yields the given explicit sequence of
    chunks from successive ``resp.content.read(n)`` calls (ignoring *n*),
    ending with an implicit EOF. Used to simulate a response that arrives
    across multiple reads rather than being sliceable from one contiguous
    buffer -- e.g. a valid JSON receipt in one chunk followed by trailing
    bytes in a later one."""
    resp = AsyncMock()
    resp.status = status

    remaining = list(chunks) + [b""]

    async def _read(n):
        return remaining.pop(0) if remaining else b""

    resp.content = MagicMock()
    resp.content.read = AsyncMock(side_effect=_read)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)

    session = AsyncMock()
    session.post = MagicMock(return_value=resp)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session, resp


class TestSchemaIsClosed:
    def test_only_target_and_question(self):
        params = PERSONA_RPC_SCHEMA["parameters"]
        assert params["additionalProperties"] is False
        assert set(params["properties"].keys()) == {"target", "question"}
        assert set(params["required"]) == {"target", "question"}

    def test_no_routing_or_credential_args(self):
        params = PERSONA_RPC_SCHEMA["parameters"]
        forbidden = {"url", "provider", "model", "caller", "header", "headers", "credential", "token", "api_key"}
        assert forbidden.isdisjoint(params["properties"].keys())


class TestValidationFailuresNeverHitNetwork:
    def test_missing_question(self):
        async def _run():
            with patch("aiohttp.ClientSession") as mock_session:
                with patch("gateway.persona_api.load_outbound_config", return_value=_outbound_config()):
                    raw = await _handle_persona_rpc({"target": "bob"})
            mock_session.assert_not_called()
            body = json.loads(raw)
            assert body["status"] == "error"
            assert body["error_code"] == "missing_question"

        asyncio.run(_run())

    def test_unknown_target(self):
        async def _run():
            with patch("aiohttp.ClientSession") as mock_session:
                with patch("gateway.persona_api.load_outbound_config", return_value=_outbound_config()):
                    raw = await _handle_persona_rpc({"target": "ghost", "question": "hi"})
            mock_session.assert_not_called()
            body = json.loads(raw)
            assert body["status"] == "error"
            assert body["error_code"] == "unknown_target"

        asyncio.run(_run())

    def test_no_outbound_config_at_all(self):
        async def _run():
            with patch("aiohttp.ClientSession") as mock_session:
                with patch("gateway.persona_api.load_outbound_config", return_value=None):
                    raw = await _handle_persona_rpc({"target": "bob", "question": "hi"})
            mock_session.assert_not_called()
            body = json.loads(raw)
            assert body["error_code"] == "unknown_target"

        asyncio.run(_run())

    def test_missing_credential(self):
        async def _run():
            outbound = _outbound_config(token="")
            with patch("aiohttp.ClientSession") as mock_session:
                with patch("gateway.persona_api.load_outbound_config", return_value=outbound):
                    raw = await _handle_persona_rpc({"target": "bob", "question": "hi"})
            mock_session.assert_not_called()
            body = json.loads(raw)
            assert body["error_code"] == "target_credential_missing"

        asyncio.run(_run())


class TestInvalidRawTargetNeverLeaked:
    """Fix: an invalid raw target value (fails normalization) must never be
    echoed back into the result or audit log, since it's attacker-shaped
    input from the model's tool-call args -- only a normalized configured
    id, or the fixed 'unknown' placeholder, is ever used."""

    def test_newline_and_secret_shaped_target_never_leaked(self):
        async def _run():
            raw_target = "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAA\ninjected-target-name"
            with patch("aiohttp.ClientSession") as mock_session:
                with patch("gateway.persona_api.load_outbound_config", return_value=_outbound_config()):
                    with patch("gateway.persona_api.audit_log") as mock_audit:
                        raw = await _handle_persona_rpc({"target": raw_target, "question": "hi"})
            mock_session.assert_not_called()
            body = json.loads(raw)
            assert body["status"] == "error"
            assert body["error_code"] == "unknown_target"
            assert body["target"] == "unknown"
            assert raw_target not in raw
            assert "sk-ant-api03" not in raw
            for call in mock_audit.call_args_list:
                target_id_arg = call.kwargs.get("target_id", "")
                assert raw_target not in target_id_arg
                assert "sk-ant-api03" not in target_id_arg

        asyncio.run(_run())

    def test_overlong_raw_target_never_leaked(self):
        async def _run():
            raw_target = "x" * 10_000
            with patch("aiohttp.ClientSession") as mock_session:
                with patch("gateway.persona_api.load_outbound_config", return_value=_outbound_config()):
                    raw = await _handle_persona_rpc({"target": raw_target, "question": "hi"})
            mock_session.assert_not_called()
            body = json.loads(raw)
            assert body["status"] == "error"
            assert body["target"] == "unknown"
            assert len(raw) < 1_000

        asyncio.run(_run())


class TestFixedRoutingFromConfig:
    def test_extra_args_are_ignored_routing_stays_fixed(self):
        """Passing url/token/etc as tool args (not part of the closed schema,
        but defensive in case a model hallucinates extra keys) must have zero
        effect — routing and credentials always come from server config."""
        async def _run():
            session, resp = _fake_session(200, _json_bytes(_ok_envelope(answer_text="42")))
            outbound = _outbound_config(url="https://bob.example.com")

            with patch("aiohttp.ClientSession", return_value=session):
                with patch("gateway.persona_api.load_outbound_config", return_value=outbound):
                    raw = await _handle_persona_rpc({
                        "target": "bob",
                        "question": "what is the answer?",
                        "url": "http://attacker.example.com",
                        "token": "attacker-supplied-token",
                        "provider": "openai",
                        "headers": {"X-Evil": "1"},
                    })

            called_url = session.post.call_args.args[0] if session.post.call_args.args else session.post.call_args.kwargs.get("url")
            assert called_url == "https://bob.example.com/api/persona/ask"
            sent_headers = session.post.call_args.kwargs["headers"]
            assert sent_headers["Authorization"] == "Bearer target-bob-token-0123456789"
            body = json.loads(raw)
            assert body["status"] == "ok"
            assert body["answer_text"] == "42"

        asyncio.run(_run())


class TestOrdinaryAnswer:
    def test_success_closed_receipt(self):
        async def _run():
            session, resp = _fake_session(200, _json_bytes(
                _ok_envelope(correlation_id="corr-1", answer_text="Paris.")
            ))
            with patch("aiohttp.ClientSession", return_value=session):
                with patch("gateway.persona_api.load_outbound_config", return_value=_outbound_config()):
                    raw = await _handle_persona_rpc({"target": "bob", "question": "capital of France?"})
            body = json.loads(raw)
            assert set(body.keys()) == {
                "object", "status", "target", "correlation_id", "answer_text", "error_code", "message",
            }
            assert body["status"] == "ok"
            assert body["target"] == "bob"
            # The remote-supplied correlation_id is never adopted -- only the
            # locally generated id (uuid4().hex) is ever returned/audited.
            assert body["correlation_id"] != "corr-1"
            assert _LOCAL_CORRELATION_ID_RE.match(body["correlation_id"])
            assert body["answer_text"] == "Paris."

        asyncio.run(_run())


class TestBoundedRawBodyRead:
    """Fix: the response body is read as raw bytes up to a hard cap BEFORE
    any JSON decoding is attempted -- resp.json() (unbounded) is never
    called."""

    def test_oversized_body_is_rejected_before_json_decode(self):
        async def _run():
            garbage = b"a" * (persona_rpc._MAX_RESPONSE_BYTES + 1000)
            session, resp = _fake_session(200, garbage)
            with patch("aiohttp.ClientSession", return_value=session):
                with patch("gateway.persona_api.load_outbound_config", return_value=_outbound_config()):
                    raw = await _handle_persona_rpc({"target": "bob", "question": "hi"})

            body = json.loads(raw)
            assert body["status"] == "error"
            assert body["error_code"] == "target_error"
            assert len(raw) < 1_000
            # Bounded read: exactly one read call for at most cap+1 bytes,
            # never an attempt to pull the entire oversized body.
            resp.content.read.assert_called_once_with(persona_rpc._MAX_RESPONSE_BYTES + 1)

        asyncio.run(_run())

    def test_valid_json_prefix_then_trailing_bytes_in_later_chunk_is_rejected(self):
        """Regression: a single resp.content.read(n) call is not guaranteed
        to return the whole body -- a compromised/misbehaving target could
        stream a fully valid, well-formed closed JSON receipt as one chunk,
        then smuggle additional trailing bytes in a later chunk. The read
        loop must keep accumulating until EOF (not stop at the first
        chunk), and the trailing bytes must cause the whole body to be
        rejected before JSON decoding succeeds -- never truncated/ignored,
        and never silently accepted as the valid receipt."""
        async def _run():
            good_envelope = _json_bytes(_ok_envelope(answer_text="42"))
            trailing = b'{"object":"hermes.persona.ask_receipt","status":"ok"}'
            session, resp = _fake_session_chunks(200, [good_envelope, trailing])
            with patch("aiohttp.ClientSession", return_value=session):
                with patch("gateway.persona_api.load_outbound_config", return_value=_outbound_config()):
                    raw = await _handle_persona_rpc({"target": "bob", "question": "hi"})

            body = json.loads(raw)
            assert body["status"] == "error"
            assert body["error_code"] == "target_error"
            assert "42" not in raw
            # The loop must have kept reading past the first (valid-looking)
            # chunk instead of stopping there.
            assert resp.content.read.call_count >= 2

        asyncio.run(_run())

    def test_truncated_body_rejected_generically(self):
        async def _run():
            async def _raise(n):
                raise ConnectionResetError("connection dropped mid-body")

            session, resp = _fake_session(200, b"")
            resp.content.read = AsyncMock(side_effect=_raise)
            with patch("aiohttp.ClientSession", return_value=session):
                with patch("gateway.persona_api.load_outbound_config", return_value=_outbound_config()):
                    raw = await _handle_persona_rpc({"target": "bob", "question": "hi"})

            body = json.loads(raw)
            assert body["status"] == "error"
            assert body["error_code"] == "target_error"

        asyncio.run(_run())


class TestRemoteEnvelopeStrictSchema:
    """Fix: the remote's closed ask-receipt envelope is re-validated as an
    exact, typed, length-bounded schema -- extras/missing keys, wrong
    object/target/types, and oversized strings (including the discarded
    correlation_id and the answer before redaction) are all rejected
    generically."""

    def test_secret_shaped_remote_correlation_id_is_never_forwarded(self):
        async def _run():
            secret = "sk-ant-api03-" + ("A" * 200)
            session, resp = _fake_session(200, _json_bytes(
                _ok_envelope(correlation_id=secret, answer_text="42")
            ))
            with patch("aiohttp.ClientSession", return_value=session):
                with patch("gateway.persona_api.load_outbound_config", return_value=_outbound_config()):
                    with patch("gateway.persona_api.audit_log") as mock_audit:
                        raw = await _handle_persona_rpc({"target": "bob", "question": "what is the answer?"})

            assert secret not in raw
            body = json.loads(raw)
            assert body["status"] == "ok"
            assert _LOCAL_CORRELATION_ID_RE.match(body["correlation_id"])

            # Nor does the attacker-controlled string reach the audit log.
            for call in mock_audit.call_args_list:
                assert secret not in call.kwargs.get("correlation_id", "")

        asyncio.run(_run())

    def test_oversized_correlation_id_is_rejected(self):
        async def _run():
            huge = "x" * (persona_rpc._MAX_CORRELATION_ID_CHARS + 1)
            session, resp = _fake_session(200, _json_bytes(
                _ok_envelope(correlation_id=huge, answer_text="42")
            ))
            with patch("aiohttp.ClientSession", return_value=session):
                with patch("gateway.persona_api.load_outbound_config", return_value=_outbound_config()):
                    raw = await _handle_persona_rpc({"target": "bob", "question": "what is the answer?"})

            body = json.loads(raw)
            assert body["status"] == "error"
            assert body["error_code"] == "target_reported_error"
            assert huge not in raw
            assert _LOCAL_CORRELATION_ID_RE.match(body["correlation_id"])

        asyncio.run(_run())

    def test_overlong_answer_is_rejected(self):
        async def _run():
            huge_answer = "x" * (persona_api.MAX_ANSWER_CHARS + 1)
            session, resp = _fake_session(200, _json_bytes(
                _ok_envelope(answer_text=huge_answer)
            ))
            with patch("aiohttp.ClientSession", return_value=session):
                with patch("gateway.persona_api.load_outbound_config", return_value=_outbound_config()):
                    raw = await _handle_persona_rpc({"target": "bob", "question": "hi"})

            body = json.loads(raw)
            assert body["status"] == "error"
            assert body["error_code"] == "target_reported_error"
            assert body["answer_text"] is None

        asyncio.run(_run())

    def test_wrong_target_in_remote_envelope_is_rejected(self):
        """A response claiming a different target identity than the one this
        call routed to must be rejected outright, not accepted as a valid
        answer from ``bob``."""
        async def _run():
            session, resp = _fake_session(200, _json_bytes(
                _ok_envelope(target="eve", answer_text="42")
            ))
            with patch("aiohttp.ClientSession", return_value=session):
                with patch("gateway.persona_api.load_outbound_config", return_value=_outbound_config()):
                    raw = await _handle_persona_rpc({"target": "bob", "question": "what is the answer?"})

            body = json.loads(raw)
            assert body["status"] == "error"
            assert body["error_code"] == "target_reported_error"
            assert body["answer_text"] is None
            # The untrusted remote-claimed target ("eve") must never be
            # adopted -- only the locally-known, requested target_id ("bob")
            # is ever returned.
            assert body["target"] == "bob"
            assert body["target"] != "eve"

        asyncio.run(_run())

    def test_malformed_envelope_missing_key_is_rejected(self):
        async def _run():
            envelope = _ok_envelope(answer_text="42")
            del envelope["target"]
            session, resp = _fake_session(200, _json_bytes(envelope))
            with patch("aiohttp.ClientSession", return_value=session):
                with patch("gateway.persona_api.load_outbound_config", return_value=_outbound_config()):
                    raw = await _handle_persona_rpc({"target": "bob", "question": "what is the answer?"})

            body = json.loads(raw)
            assert body["status"] == "error"
            assert body["error_code"] == "target_reported_error"

        asyncio.run(_run())

    def test_envelope_with_extra_key_is_rejected(self):
        async def _run():
            envelope = _ok_envelope(answer_text="42", extra_field="surprise")
            session, resp = _fake_session(200, _json_bytes(envelope))
            with patch("aiohttp.ClientSession", return_value=session):
                with patch("gateway.persona_api.load_outbound_config", return_value=_outbound_config()):
                    raw = await _handle_persona_rpc({"target": "bob", "question": "what is the answer?"})

            body = json.loads(raw)
            assert body["status"] == "error"
            assert body["error_code"] == "target_reported_error"

        asyncio.run(_run())

    def test_wrong_object_value_is_rejected(self):
        async def _run():
            envelope = _ok_envelope(answer_text="42", object="not.the.right.object")
            session, resp = _fake_session(200, _json_bytes(envelope))
            with patch("aiohttp.ClientSession", return_value=session):
                with patch("gateway.persona_api.load_outbound_config", return_value=_outbound_config()):
                    raw = await _handle_persona_rpc({"target": "bob", "question": "what is the answer?"})

            body = json.loads(raw)
            assert body["status"] == "error"
            assert body["error_code"] == "target_reported_error"

        asyncio.run(_run())

    def test_malformed_envelope_wrong_type_target_is_rejected(self):
        async def _run():
            envelope = _ok_envelope(answer_text="42", target=12345)
            session, resp = _fake_session(200, _json_bytes(envelope))
            with patch("aiohttp.ClientSession", return_value=session):
                with patch("gateway.persona_api.load_outbound_config", return_value=_outbound_config()):
                    raw = await _handle_persona_rpc({"target": "bob", "question": "what is the answer?"})

            body = json.loads(raw)
            assert body["status"] == "error"
            assert body["error_code"] == "target_reported_error"

        asyncio.run(_run())

    def test_malformed_envelope_wrong_type_answer_is_rejected(self):
        async def _run():
            envelope = _ok_envelope(answer_text=12345)
            session, resp = _fake_session(200, _json_bytes(envelope))
            with patch("aiohttp.ClientSession", return_value=session):
                with patch("gateway.persona_api.load_outbound_config", return_value=_outbound_config()):
                    raw = await _handle_persona_rpc({"target": "bob", "question": "what is the answer?"})

            body = json.loads(raw)
            assert body["status"] == "error"
            assert body["error_code"] == "target_reported_error"

        asyncio.run(_run())

    def test_envelope_with_error_code_set_on_ok_status_is_rejected(self):
        async def _run():
            envelope = _ok_envelope(answer_text="42", error_code="something")
            session, resp = _fake_session(200, _json_bytes(envelope))
            with patch("aiohttp.ClientSession", return_value=session):
                with patch("gateway.persona_api.load_outbound_config", return_value=_outbound_config()):
                    raw = await _handle_persona_rpc({"target": "bob", "question": "what is the answer?"})

            body = json.loads(raw)
            assert body["status"] == "error"
            assert body["error_code"] == "target_reported_error"

        asyncio.run(_run())


class TestRemoteAnswerRedaction:
    def test_secret_shaped_remote_answer_is_redacted(self):
        async def _run():
            secret = "sk-remote-leaked-secret-1234567890"
            session, resp = _fake_session(200, _json_bytes(
                _ok_envelope(answer_text=f"key: {secret}")
            ))
            with patch("aiohttp.ClientSession", return_value=session):
                with patch("gateway.persona_api.load_outbound_config", return_value=_outbound_config()):
                    raw = await _handle_persona_rpc({"target": "bob", "question": "leak a secret"})
            assert secret not in raw

        asyncio.run(_run())

    def test_redaction_failure_fails_closed(self):
        async def _run():
            session, resp = _fake_session(200, _json_bytes(
                _ok_envelope(answer_text="an ordinary answer")
            ))
            with patch("aiohttp.ClientSession", return_value=session), \
                 patch("gateway.persona_api.load_outbound_config", return_value=_outbound_config()), \
                 patch("gateway.persona_api.redact_sensitive_text", side_effect=RuntimeError("boom")):
                raw = await _handle_persona_rpc({"target": "bob", "question": "hi"})
            body = json.loads(raw)
            assert body["status"] == "error"
            assert body["error_code"] == "redaction_failed"
            assert "an ordinary answer" not in raw

        asyncio.run(_run())


class TestRemoteAndNetworkFailuresNeverLeakRaw:
    def test_remote_non_200_status(self):
        async def _run():
            session, resp = _fake_session(500, _json_bytes({"error": "boom"}))
            with patch("aiohttp.ClientSession", return_value=session):
                with patch("gateway.persona_api.load_outbound_config", return_value=_outbound_config()):
                    raw = await _handle_persona_rpc({"target": "bob", "question": "hi"})
            body = json.loads(raw)
            assert body["status"] == "error"
            assert body["error_code"] == "target_error"

        asyncio.run(_run())

    def test_remote_reports_error_status(self):
        async def _run():
            envelope = _ok_envelope(
                answer_text=None, error_code="ask_failed", message="failed", status="error",
            )
            session, resp = _fake_session(200, _json_bytes(envelope))
            with patch("aiohttp.ClientSession", return_value=session):
                with patch("gateway.persona_api.load_outbound_config", return_value=_outbound_config()):
                    raw = await _handle_persona_rpc({"target": "bob", "question": "hi"})
            body = json.loads(raw)
            assert body["status"] == "error"
            assert body["error_code"] == "target_reported_error"

        asyncio.run(_run())

    def test_network_exception_no_traceback_leak(self):
        async def _run():
            with patch("aiohttp.ClientSession", side_effect=RuntimeError(
                "Traceback (most recent call last): connection refused to https://bob.example.com secret=sk-abc123456789"
            )):
                with patch("gateway.persona_api.load_outbound_config", return_value=_outbound_config()):
                    raw = await _handle_persona_rpc({"target": "bob", "question": "hi"})
            body = json.loads(raw)
            assert body["status"] == "error"
            assert body["error_code"] == "target_unreachable"
            assert "Traceback" not in raw
            assert "sk-abc123456789" not in raw

        asyncio.run(_run())

    def test_malformed_json_response(self):
        async def _run():
            session, resp = _fake_session(200, b"not valid json {")
            with patch("aiohttp.ClientSession", return_value=session):
                with patch("gateway.persona_api.load_outbound_config", return_value=_outbound_config()):
                    raw = await _handle_persona_rpc({"target": "bob", "question": "hi"})
            body = json.loads(raw)
            assert body["status"] == "error"
            assert body["error_code"] == "target_error"

        asyncio.run(_run())
