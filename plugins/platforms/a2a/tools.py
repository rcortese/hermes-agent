"""Restricted Moss-to-Denholm A2A client tool.

The model-visible A2A contract is intentionally one-way and single-purpose:
``a2a_call(message)`` sends a task only to the fixed ``denholm`` peer at
``http://denholm:9900``.  The endpoint is not configurable by a caller, Agent
Card discovery is not performed, and HTTP redirects are rejected.

The bearer credential is read from the configured ``a2a_agents.denholm.auth``
entry; it is never accepted as a tool argument or emitted in a schema.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, TypedDict

from . import protocol, security

logger = logging.getLogger(__name__)

_DENHOLM_ALIAS = "denholm"
_DENHOLM_ENDPOINT = "http://denholm:9900"
_DEFAULT_TIMEOUT = 120


def _load_config() -> dict:
    try:
        from hermes_cli.config import load_config

        return load_config() or {}
    except Exception:
        return {}


def _denholm_auth() -> dict:
    """Return only Denholm's configured bearer reference/value mapping."""
    peers = _load_config().get("a2a_agents") or {}
    entry = peers.get(_DENHOLM_ALIAS) or {}
    auth = entry.get("auth") or {}
    if not isinstance(auth, dict) or auth.get("type") != "bearer":
        return {}
    token = str(auth.get("token") or "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _denholm_timeout() -> int:
    """Read a bounded transport timeout without granting endpoint control."""
    peers = _load_config().get("a2a_agents") or {}
    entry = peers.get(_DENHOLM_ALIAS) or {}
    try:
        return max(1, int(entry.get("timeout", _DEFAULT_TIMEOUT)))
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    """Make redirect rejection explicit instead of inheriting urllib defaults."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise urllib.error.HTTPError(req.full_url, code, "redirects are not permitted", headers, fp)


_NO_REDIRECT_OPENER = urllib.request.build_opener(_RejectRedirects())


def _post_denholm(body: dict, headers: dict, timeout: int) -> dict:
    data = json.dumps(body).encode("utf-8")
    request_headers = {
        "Content-Type": "application/json",
        "A2A-Version": protocol.PROTOCOL_VERSION,
        **headers,
    }
    req = urllib.request.Request(
        _DENHOLM_ENDPOINT,
        data=data,
        headers=request_headers,
        method="POST",
    )
    with _NO_REDIRECT_OPENER.open(req, timeout=timeout) as response:  # noqa: S310 (fixed endpoint)
        return json.loads(response.read().decode("utf-8"))


def _reply_text_from_result(result: Any) -> str:
    result = protocol.unwrap_send_message_response(result)
    if not isinstance(result, dict):
        return str(result)
    for artifact in result.get("artifacts", []) or []:
        text = protocol.extract_text(artifact)
        if text:
            return text
    status = result.get("status", {}) or {}
    if status.get("message"):
        return protocol.extract_text(status["message"])
    return protocol.extract_text(result)


def _short_state(state: str) -> str:
    return state.replace("TASK_STATE_", "").replace("_", "-").lower() if state else ""


def a2a_call(args: dict, **_: Any) -> str:
    """Send ``message`` to the fixed Denholm peer and return its reply."""
    if any(key in args for key in ("agent", "agent_name", "name", "url", "endpoint")):
        return "Error: caller-selected endpoint or peer is not permitted; a2a_call always targets Denholm."
    message = str(args.get("message") or args.get("text") or args.get("task") or "").strip()
    context_id = str(args.get("context_id") or args.get("contextId") or "").strip()
    if not message:
        return "Error: 'message' is required."

    context_id = context_id or protocol.new_context_id()
    safe_message = security.redact_outbound(message)
    request_id = protocol.new_task_id()
    rpc_body = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "SendMessage",
        "params": {
            "message": protocol.text_message(protocol.ROLE_USER, safe_message, context_id=context_id),
        },
    }

    security.audit("outbound", _DENHOLM_ALIAS, request_id, safe_message)
    protocol.persist_message(context_id, "user", safe_message, request_id)
    protocol.metrics.outbound_total += 1
    try:
        response = _post_denholm(rpc_body, _denholm_auth(), _denholm_timeout())
    except urllib.error.HTTPError as exc:
        if exc.code in (301, 302, 303, 307, 308):
            return "Error: Denholm redirect rejected; fixed endpoint authority was preserved."
        if exc.code in (401, 403):
            return "Error: Denholm rejected auth (HTTP %s). Check its configured bearer reference." % exc.code
        if exc.code == 429:
            return "Error: Denholm rate limited us (HTTP 429). Retry later."
        return f"Error: call to Denholm failed — HTTP {exc.code}."
    except ValueError as exc:
        return f"Error: Denholm returned an error: {exc}"
    except Exception as exc:
        return f"Error: call to Denholm failed — {exc}"

    if "error" in response:
        error = response["error"]
        return f"Error: Denholm returned an error: {error.get('message', error)}"

    payload = protocol.unwrap_send_message_response(response.get("result", {}))
    reply = _reply_text_from_result(payload)
    reply_context_id, state = context_id, ""
    if isinstance(payload, dict):
        reply_context_id = payload.get("contextId", context_id)
        state = (payload.get("status") or {}).get("state", "")
    protocol.persist_message(reply_context_id, "agent", reply, request_id)
    protocol.metrics.inbound_total += 1

    header = f"[denholm · context {reply_context_id}"
    if state:
        header += f" · {_short_state(state)}"
    header += "]"
    body = reply or "(no text reply)"
    if state == protocol.STATE_INPUT_REQUIRED:
        body += f"\n\n(Denholm needs more input; call a2a_call with context_id '{reply_context_id}'.)"
    return f"{header}\n{body}"


_FunctionSchema = TypedDict(
    "_FunctionSchema",
    {"name": str, "description": str, "parameters": dict[str, Any]},
    total=False,
)
_ToolSchema = TypedDict("_ToolSchema", {"type": str, "function": _FunctionSchema}, total=False)

_SCHEMAS: dict[str, _ToolSchema] = {
    "a2a_call": {
        "type": "function",
        "function": {
            "name": "a2a_call",
            "description": "Send a task to the fixed Denholm A2A peer and return its reply.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "The task or message to send to Denholm.",
                    },
                    "context_id": {
                        "type": "string",
                        "description": "Optional prior context id for a continued Denholm exchange.",
                    },
                },
                "required": ["message"],
                "additionalProperties": False,
            },
        },
    },
}

_HANDLERS = {"a2a_call": a2a_call}


def register_tools(ctx) -> None:
    """Register the sole model-visible restricted A2A client tool."""
    schema = _SCHEMAS["a2a_call"].get("function")
    assert schema is not None
    ctx.register_tool(
        name="a2a_call",
        toolset="a2a",
        schema=schema,
        handler=a2a_call,
        description=schema["description"],
        emoji="\U0001f9e9",
    )
