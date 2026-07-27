"""``persona_rpc`` — ask-only Persona-to-persona (A2A) RPC tool.

Caller-side half of the Persona API (see ``gateway/persona_api.py`` for the
shared config/authorization helpers and ``gateway/platforms/api_server.py``'s
``POST /api/persona/ask`` for the target-side handler this tool calls).

Design constraints (all deliberate, not oversights):

- The tool schema is CLOSED: only ``target`` and ``question``. There is no
  url/provider/model/caller/header/credential argument — routing and
  credentials are fixed by server-side config (``persona_api.outbound.targets``
  in config.yaml), never by the model's tool-call arguments.
- The tool result is a closed, typed receipt (status/target/correlation_id/
  answer_text) — never a raw exception string, traceback, or the remote's
  unvalidated JSON body.
- Redaction of the returned answer is fail-closed: if redaction itself
  raises, the tool reports a generic failure rather than risk emitting
  unredacted text.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

# Hard cap on the raw remote response body, enforced by reading at most this
# many bytes off the wire BEFORE any JSON decoding is attempted -- an
# oversized or slow-trickling body is rejected generically without ever
# being handed to json.loads. Sized well above a legitimate MAX_ANSWER_CHARS
# answer (worst case ~4 bytes/char UTF-8) plus envelope overhead.
_MAX_RESPONSE_BYTES = 256 * 1024

# The remote's closed ask-receipt envelope must contain EXACTLY these keys
# (see gateway.persona_api.build_receipt) -- no extras, none missing.
_ASK_RECEIPT_KEYS = frozenset({
    "object", "status", "target", "correlation_id", "answer_text", "error_code", "message",
})
_ASK_RECEIPT_OBJECT = "hermes.persona.ask_receipt"
# correlation_id is discarded (never adopted, see the caller) but is still
# schema-validated -- an oversized/wrong-typed value fails the whole
# envelope rather than being silently ignored.
_MAX_CORRELATION_ID_CHARS = 256


def _validate_remote_ask_envelope(
    payload: Dict[str, Any], *, expected_target: str, max_answer_chars: int,
) -> Any:
    """Validate the remote's closed ask-receipt envelope end to end.

    Returns the validated ``answer_text`` string on success, or ``None`` for
    any failure -- envelope missing/extra keys, wrong ``object``, non-"ok"
    status, a target other than the one this call routed to, wrong field
    types, or any string field (including the discarded ``correlation_id``)
    exceeding its bounded max length. Every failure is treated identically
    by the caller (a generic "target did not return a valid answer"
    failure), so a compromised/misconfigured/misrouted target can't
    distinguish which check tripped. ``correlation_id`` is deliberately
    never adopted from the remote (see the caller in ``_handle_persona_rpc``)
    even though its shape is validated here.
    """
    if not isinstance(payload, dict) or set(payload.keys()) != _ASK_RECEIPT_KEYS:
        return None
    if payload.get("object") != _ASK_RECEIPT_OBJECT:
        return None
    if payload.get("status") != "ok":
        return None

    remote_target = payload.get("target")
    if not isinstance(remote_target, str) or remote_target != expected_target:
        return None

    correlation_id = payload.get("correlation_id")
    if (
        not isinstance(correlation_id, str)
        or not correlation_id
        or len(correlation_id) > _MAX_CORRELATION_ID_CHARS
    ):
        return None

    answer = payload.get("answer_text")
    if not isinstance(answer, str) or not answer or len(answer) > max_answer_chars:
        return None

    # A genuine success receipt (see build_receipt / _handle_persona_ask)
    # always leaves error_code/message unset -- a remote pairing status="ok"
    # with either set does not match the documented contract.
    if payload.get("error_code") is not None or payload.get("message") is not None:
        return None

    return answer


async def _handle_persona_rpc(args: Dict[str, Any], **kwargs: Any) -> str:
    from gateway import persona_api

    target_raw = args.get("target")
    question_raw = args.get("question")
    target_id = persona_api.normalize_id(target_raw)
    correlation_id = persona_api.new_correlation_id()
    # Never surface the caller's raw, unnormalized target value anywhere
    # (result or audit log) -- an invalid target could be arbitrarily
    # shaped (newlines, secret-looking strings). Only a normalized,
    # character-restricted target id is ever used, or a fixed safe
    # placeholder when normalization fails outright.
    display_target = target_id or "unknown"

    if not isinstance(question_raw, str) or not question_raw.strip():
        return json.dumps(persona_api.build_receipt(
            status="error", target=display_target, correlation_id=correlation_id,
            error_code="missing_question", message="A non-empty 'question' is required.",
        ), ensure_ascii=False)

    outbound = persona_api.load_outbound_config()
    if outbound is None or not target_id or target_id not in outbound.targets:
        persona_api.audit_log(
            "rpc", target_id=display_target, correlation_id=correlation_id, outcome="unknown_target",
        )
        return json.dumps(persona_api.build_receipt(
            status="error", target=display_target, correlation_id=correlation_id,
            error_code="unknown_target", message="Target is not configured.",
        ), ensure_ascii=False)

    remote = outbound.targets[target_id]
    if not remote.token:
        persona_api.audit_log(
            "rpc", target_id=target_id, correlation_id=correlation_id, outcome="credential_missing",
        )
        return json.dumps(persona_api.build_receipt(
            status="error", target=target_id, correlation_id=correlation_id,
            error_code="target_credential_missing", message="No credential configured for this target.",
        ), ensure_ascii=False)

    question = question_raw.strip()[: persona_api.MAX_QUESTION_CHARS]

    try:
        import aiohttp

        url = f"{remote.url}/api/persona/ask"
        headers = {
            "Authorization": f"Bearer {remote.token}",
            "Content-Type": "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=persona_api.ASK_HTTP_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json={"question": question}, headers=headers, timeout=timeout,
            ) as resp:
                status_code = resp.status
                payload = None
                try:
                    # Bounded raw-byte read BEFORE any JSON decoding: never
                    # call resp.json() directly, since it has no size cap
                    # and would decode an arbitrarily large/slow body. A
                    # single resp.content.read(n) call is NOT guaranteed to
                    # return the whole body even when more is available (it
                    # may return one chunk at a time), so this loops,
                    # accumulating chunks until EOF (an empty chunk) or the
                    # cap is exceeded -- it never holds more than
                    # _MAX_RESPONSE_BYTES + 1 bytes in memory at once.
                    chunks = []
                    total = 0
                    while True:
                        chunk = await resp.content.read(_MAX_RESPONSE_BYTES + 1 - total)
                        if not chunk:
                            break
                        chunks.append(chunk)
                        total += len(chunk)
                        if total > _MAX_RESPONSE_BYTES:
                            break
                    if total <= _MAX_RESPONSE_BYTES:
                        raw_body = b"".join(chunks)
                        # json.loads rejects any trailing non-whitespace
                        # bytes after the first valid JSON value ("Extra
                        # data") -- this is what catches a body that arrives
                        # as a valid-looking JSON receipt in an early chunk
                        # followed by additional trailing bytes in a later
                        # chunk, since the whole accumulated body must be
                        # nothing but that one JSON value.
                        decoded = json.loads(raw_body.decode("utf-8"))
                        if isinstance(decoded, dict):
                            payload = decoded
                except Exception:
                    # Oversized, truncated (connection dropped mid-body),
                    # non-UTF8, malformed JSON, or valid-JSON-plus-trailing-
                    # bytes are all indistinguishable here on purpose --
                    # payload stays None and every case is rejected
                    # identically below.
                    payload = None
    except Exception:
        # Never surface the raw exception/traceback to the model — network
        # errors can embed hostnames/paths that belong in server logs only.
        logger.exception("persona_rpc: request to target=%s failed", target_id)
        persona_api.audit_log(
            "rpc", target_id=target_id, correlation_id=correlation_id, outcome="unreachable",
        )
        return json.dumps(persona_api.build_receipt(
            status="error", target=target_id, correlation_id=correlation_id,
            error_code="target_unreachable", message="Could not reach the target.",
        ), ensure_ascii=False)

    if status_code != 200 or not isinstance(payload, dict):
        persona_api.audit_log(
            "rpc", target_id=target_id, correlation_id=correlation_id, outcome=f"http_{status_code}",
        )
        return json.dumps(persona_api.build_receipt(
            status="error", target=target_id, correlation_id=correlation_id,
            error_code="target_error", message="Target returned an error.",
        ), ensure_ascii=False)

    # Re-validate the remote's closed schema — never trust it blindly, even
    # though it is itself a Hermes /api/persona/ask response. A compromised
    # or misconfigured target's JSON shape is untrusted input here.
    #
    # Note the remote's "correlation_id" field is never read: it is the
    # remote's own free-form output, and adopting it here would let a
    # malicious/compromised target inject an unbounded or secret-shaped
    # string straight into this tool's result and audit log. This call
    # always reports back the correlation_id generated locally above.
    remote_answer = _validate_remote_ask_envelope(
        payload, expected_target=target_id, max_answer_chars=persona_api.MAX_ANSWER_CHARS,
    )
    if remote_answer is None:
        persona_api.audit_log(
            "rpc", target_id=target_id, correlation_id=correlation_id,
            outcome="target_reported_error",
        )
        return json.dumps(persona_api.build_receipt(
            status="error", target=target_id, correlation_id=correlation_id,
            error_code="target_reported_error", message="Target did not return a valid answer.",
        ), ensure_ascii=False)

    # Fail-closed redaction on the CALLER side too — defense in depth against
    # a remote target's answer reflecting back anything secret-shaped.
    answer_text = persona_api.safe_redact(remote_answer)
    if answer_text is None:
        persona_api.audit_log(
            "rpc", target_id=target_id, correlation_id=correlation_id, outcome="redaction_failed",
        )
        return json.dumps(persona_api.build_receipt(
            status="error", target=target_id, correlation_id=correlation_id,
            error_code="redaction_failed", message="Answer could not be safely returned.",
        ), ensure_ascii=False)
    answer_text = answer_text[: persona_api.MAX_ANSWER_CHARS]

    persona_api.audit_log(
        "rpc", target_id=target_id, correlation_id=correlation_id, outcome="ok",
    )
    return json.dumps(persona_api.build_receipt(
        status="ok", target=target_id, correlation_id=correlation_id, answer_text=answer_text,
    ), ensure_ascii=False)


def _check_persona_rpc_available() -> bool:
    """Tool is only available when at least one outbound target is configured."""
    try:
        from gateway import persona_api
        return persona_api.load_outbound_config() is not None
    except Exception:
        return False


PERSONA_RPC_SCHEMA = {
    "name": "persona_rpc",
    "description": (
        "Ask another configured Hermes persona (a 'target') a single question "
        "over the ask-only Persona RPC channel. Routing and credentials for "
        "each target are fixed by server-side config — this tool only lets "
        "you pick which configured target to ask and what to ask it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": "Name of a configured persona target to ask.",
            },
            "question": {
                "type": "string",
                "description": "The question to ask the target persona.",
            },
        },
        "required": ["target", "question"],
        "additionalProperties": False,
    },
}


from tools.registry import registry

registry.register(
    name="persona_rpc",
    toolset="persona",
    schema=PERSONA_RPC_SCHEMA,
    handler=_handle_persona_rpc,
    check_fn=_check_persona_rpc_available,
    is_async=True,
    description="Ask another configured Hermes persona a question over the ask-only Persona RPC channel.",
    emoji="🛰️",
)
