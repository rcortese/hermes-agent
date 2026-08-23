"""Restricted Moss-to-Denholm A2A client tool.

The model-visible A2A contract exposes five familiar operations, all restricted
to one fixed peer: ``a2a_call`` and ``a2a_orchestrate`` send one task only to
``denholm`` at ``http://denholm:9900``; ``a2a_discover`` and ``a2a_list`` only
describe that local fixed authority; and ``a2a_history`` reads local persisted
conversation records. The endpoint is not configurable by a caller, remote
Agent Card discovery is not performed, fan-out is unavailable, and HTTP
redirects are rejected.

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
    if any(key in args for key in ("agent", "agent_name", "name", "url", "endpoint", "path", "credential", "token")):
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


def _fixed_peer_description() -> dict[str, str]:
    """Return the sole peer descriptor without contacting a remote endpoint."""
    return {
        "peer": _DENHOLM_ALIAS,
        "endpoint": _DENHOLM_ENDPOINT,
        "discovery": "disabled",
    }


def a2a_discover(args: dict, **_: Any) -> str:
    """Describe the fixed Denholm authority; never perform remote discovery."""
    if args:
        return "Error: a2a_discover has no caller-selected routing options."
    return json.dumps(_fixed_peer_description())


def a2a_list(args: dict, **_: Any) -> str:
    """List the one approved peer without querying or routing to remote agents."""
    if args:
        return "Error: a2a_list has no caller-selected routing options."
    return json.dumps({"peers": [_fixed_peer_description()]})


def a2a_history(args: dict, **_: Any) -> str:
    """Read bounded local conversation history for an explicit context id."""
    if any(key in args for key in ("agent", "agent_name", "name", "url", "endpoint", "path", "credential", "token")):
        return "Error: caller-selected endpoint, peer, or path is not permitted."
    context_id = str(args.get("context_id") or args.get("contextId") or "").strip()
    if not context_id:
        return "Error: 'context_id' is required."
    try:
        limit = min(50, max(1, int(args.get("limit", 50))))
    except (TypeError, ValueError):
        return "Error: 'limit' must be an integer."
    return json.dumps({
        "peer": _DENHOLM_ALIAS,
        "context_id": context_id,
        "messages": protocol.load_conversation(context_id, limit=limit),
    })


def a2a_orchestrate(args: dict, **kwargs: Any) -> str:
    """Delegate one task to Denholm; this is not multi-peer orchestration."""
    if any(key in args for key in ("agent", "agent_name", "name", "agents", "peers", "targets", "fanout", "url", "endpoint", "path", "credential", "token")):
        return "Error: fan-out, caller-selected routing, and caller credentials are not permitted."
    return a2a_call(args, **kwargs)


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
            "description": "Send one task to the fixed Denholm A2A peer and return its reply.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "The task or message to send to Denholm."},
                    "context_id": {"type": "string", "description": "Optional prior context id for a continued Denholm exchange."},
                },
                "required": ["message"],
                "additionalProperties": False,
            },
        },
    },
    "a2a_discover": {
        "type": "function",
        "function": {
            "name": "a2a_discover",
            "description": "Describe the fixed Denholm authority; no remote Agent Card lookup is performed.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    "a2a_list": {
        "type": "function",
        "function": {
            "name": "a2a_list",
            "description": "List the one approved Denholm peer; no remote peer enumeration is performed.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    "a2a_history": {
        "type": "function",
        "function": {
            "name": "a2a_history",
            "description": "Read bounded local persisted history for a Denholm context; no remote history request is made.",
            "parameters": {
                "type": "object",
                "properties": {
                    "context_id": {"type": "string", "description": "The locally persisted Denholm context id."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Maximum local messages to return (default 50)."},
                },
                "required": ["context_id"],
                "additionalProperties": False,
            },
        },
    },
    "a2a_orchestrate": {
        "type": "function",
        "function": {
            "name": "a2a_orchestrate",
            "description": "Delegate one task to fixed Denholm only; this never fans out or selects peers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "The single task to delegate to Denholm."},
                    "context_id": {"type": "string", "description": "Optional prior context id for the one Denholm exchange."},
                },
                "required": ["message"],
                "additionalProperties": False,
            },
        },
    },
}

_HANDLERS = {
    "a2a_call": a2a_call,
    "a2a_discover": a2a_discover,
    "a2a_list": a2a_list,
    "a2a_history": a2a_history,
    "a2a_orchestrate": a2a_orchestrate,
}


def register_tools(ctx) -> None:
    """Register the five core-owned, fixed-Denholm A2A model tools."""
    for name, handler in _HANDLERS.items():
        schema = _SCHEMAS[name].get("function")
        assert schema is not None
        ctx.register_tool(
            name=name,
            toolset="a2a",
            schema=schema,
            handler=handler,
            description=schema["description"],
            emoji="\U0001f9e9",
        )
