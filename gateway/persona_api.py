"""Persona-to-persona (A2A) ask-only RPC: shared config, authorization, and
response-envelope helpers.

Two roles share this module:

- **Target** (``gateway/platforms/api_server.py``'s ``POST /api/persona/ask``
  route): another Hermes instance authenticates as a *caller* and asks this
  instance (the *target*) a question. Authorization is a deny-by-default
  ``caller -> target -> ask`` matrix: the caller identity is derived from the
  authenticated credential (never from the request body), and the target
  identity is this server's own configured identity (also never from the
  request body).
- **Caller** (``tools/persona_rpc.py``): this instance asks another Hermes
  instance's target a question, using a server-side credential resolved from
  fixed config -- never from tool-call arguments.

v1 scope is deliberately narrow: "ask" is the only operation. There is no
run/status/stop/artifact surface here -- see ``docs`` for the larger A2A
cutover plan.

Config lives under a top-level ``persona_api`` key in config.yaml::

    persona_api:
      self_target: "atlas"              # this instance's own target identity
      inbound:
        callers:
          mercury:
            allow_targets: ["atlas"]    # deny-by-default matrix row
            token: "..."                # optional inline (dev/test only);
                                         # production convention is the
                                         # PERSONA_CALLER_<ID>_TOKEN env var
      outbound:
        targets:
          bob:
            url: "https://bob.example.com"
            token: "..."                # optional inline; production
                                         # convention is PERSONA_TARGET_<ID>_TOKEN
"""

from __future__ import annotations

import hmac
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Optional

from agent.redact import redact_sensitive_text

logger = logging.getLogger(__name__)

# Only "ask" exists in v1 -- no run/status/stop/artifact ops.
ASK_OP = "ask"

MAX_QUESTION_CHARS = 8_000
MAX_ANSWER_CHARS = 20_000
ASK_HTTP_TIMEOUT_SECONDS = 30

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def normalize_id(raw: Any) -> str:
    """Normalize a caller/target identifier; returns "" if invalid.

    Deliberately strict (lowercase alnum/_/- only, 1-64 chars) since these
    identifiers are used to derive env-var names and appear in audit logs.
    """
    if not isinstance(raw, str):
        return ""
    candidate = raw.strip().lower()
    if not _ID_RE.match(candidate):
        return ""
    return candidate


def _env_var_name(prefix: str, ident: str) -> str:
    """Derive the conventional env-var name for a persona credential.

    e.g. ``_env_var_name("PERSONA_CALLER", "mercury")`` ->
    ``"PERSONA_CALLER_MERCURY_TOKEN"``. This naming convention is also
    what ``tools.environments.local._is_hermes_internal_secret`` matches to
    strip these credentials from every spawned subprocess/terminal env.
    """
    return f"{prefix}_{ident.upper().replace('-', '_')}_TOKEN"


def _resolve_credential(entry: Dict[str, Any], env_name: str) -> str:
    """Resolve a persona credential: inline ``token`` wins, else env var.

    Inline ``token`` in config.yaml is supported for dev/test convenience,
    mirroring the existing ``model_routes.api_key`` / ``PlatformConfig.token``
    precedent elsewhere in the gateway config. Production deployments should
    prefer the env var so the secret never round-trips through config.yaml.
    """
    inline = entry.get("token")
    if isinstance(inline, str) and inline:
        return inline
    try:
        from agent.secret_scope import get_secret

        return get_secret(env_name) or ""
    except Exception:
        return ""


@dataclass(frozen=True)
class CallerRule:
    caller_id: str
    allow_targets: FrozenSet[str]
    token: str


@dataclass(frozen=True)
class RemoteTarget:
    target_id: str
    url: str
    token: str


@dataclass(frozen=True)
class InboundConfig:
    self_target: str
    callers: Dict[str, CallerRule] = field(default_factory=dict)


@dataclass(frozen=True)
class OutboundConfig:
    targets: Dict[str, RemoteTarget] = field(default_factory=dict)


def _load_persona_section(cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if cfg is None:
        try:
            from hermes_cli.config import load_config_readonly

            cfg = load_config_readonly()
        except Exception:
            return {}
    section = cfg.get("persona_api") if isinstance(cfg, dict) else None
    return section if isinstance(section, dict) else {}


def load_inbound_config(cfg: Optional[Dict[str, Any]] = None) -> Optional[InboundConfig]:
    """Load and validate the target-side (inbound ask) persona config.

    Returns ``None`` when persona_api / self_target / any callers are not
    configured -- the ask route treats that as "feature disabled" (404),
    which is also the deny-by-default posture: no config means nothing is
    ever authorized.
    """
    section = _load_persona_section(cfg)
    if not section:
        return None

    self_target = normalize_id(section.get("self_target"))
    if not self_target:
        return None

    inbound = section.get("inbound")
    if not isinstance(inbound, dict):
        return None
    raw_callers = inbound.get("callers")
    if not isinstance(raw_callers, dict) or not raw_callers:
        return None

    callers: Dict[str, CallerRule] = {}
    for raw_id, raw_rule in raw_callers.items():
        caller_id = normalize_id(raw_id)
        if not caller_id or not isinstance(raw_rule, dict):
            logger.warning("persona_api.inbound.callers: dropping invalid entry %r", raw_id)
            continue
        raw_targets = raw_rule.get("allow_targets")
        if isinstance(raw_targets, str):
            raw_targets = [raw_targets]
        allow_targets = frozenset(
            normalize_id(t) for t in (raw_targets or []) if normalize_id(t)
        )
        token = _resolve_credential(raw_rule, _env_var_name("PERSONA_CALLER", caller_id))
        if not token:
            logger.warning(
                "persona_api.inbound.callers.%s: no credential resolved (set %s); "
                "this caller can never authenticate",
                caller_id, _env_var_name("PERSONA_CALLER", caller_id),
            )
        callers[caller_id] = CallerRule(caller_id=caller_id, allow_targets=allow_targets, token=token)

    if not callers:
        return None

    dupes = _duplicate_credential_caller_ids(callers)
    if dupes:
        # Fail closed: two callers must never be able to share a credential
        # (a shared token would let one caller authenticate as either, and
        # resolve_caller's caller-selection would otherwise become an
        # implementation detail instead of a security boundary). Treated as
        # config-invalid -- the whole inbound feature is disabled (404) --
        # rather than silently dropping just the colliding entries, so the
        # failure is deterministic and doesn't depend on dict iteration
        # order. Only caller identifiers are logged, never token values.
        logger.warning(
            "persona_api.inbound.callers: duplicate credential shared by %s; "
            "failing closed (persona ask disabled) until each caller has a "
            "distinct token",
            ", ".join(sorted(dupes)),
        )
        return None

    return InboundConfig(self_target=self_target, callers=callers)


def _duplicate_credential_caller_ids(callers: Dict[str, "CallerRule"]) -> FrozenSet[str]:
    """Return the caller_ids that share a credential with at least one other
    configured caller, or an empty frozenset if every credential is distinct.

    Compares every pair of resolved tokens with ``hmac.compare_digest``
    (constant-time per comparison, so the check itself can't leak which byte
    position first differed) and never logs, prints, or otherwise persists
    a token value -- only the caller identifiers involved are ever surfaced.
    """
    ids = [caller_id for caller_id, rule in callers.items() if rule.token]
    dupes: set = set()
    for i, id_a in enumerate(ids):
        token_a = callers[id_a].token
        for id_b in ids[i + 1 :]:
            token_b = callers[id_b].token
            try:
                same = hmac.compare_digest(token_a.encode(), token_b.encode())
            except (UnicodeEncodeError, TypeError):
                continue
            if same:
                dupes.add(id_a)
                dupes.add(id_b)
    return frozenset(dupes)


def load_outbound_config(cfg: Optional[Dict[str, Any]] = None) -> Optional[OutboundConfig]:
    """Load and validate the caller-side (outbound persona_rpc) config."""
    section = _load_persona_section(cfg)
    if not section:
        return None
    outbound = section.get("outbound")
    if not isinstance(outbound, dict):
        return None
    raw_targets = outbound.get("targets")
    if not isinstance(raw_targets, dict) or not raw_targets:
        return None

    targets: Dict[str, RemoteTarget] = {}
    for raw_id, raw_entry in raw_targets.items():
        target_id = normalize_id(raw_id)
        if not target_id or not isinstance(raw_entry, dict):
            logger.warning("persona_api.outbound.targets: dropping invalid entry %r", raw_id)
            continue
        url = str(raw_entry.get("url") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            logger.warning("persona_api.outbound.targets.%s: invalid/missing url", target_id)
            continue
        token = _resolve_credential(raw_entry, _env_var_name("PERSONA_TARGET", target_id))
        targets[target_id] = RemoteTarget(target_id=target_id, url=url.rstrip("/"), token=token)

    if not targets:
        return None
    return OutboundConfig(targets=targets)


def resolve_caller(inbound: InboundConfig, presented_token: str) -> Optional[str]:
    """Return the caller_id whose credential matches *presented_token*, else None.

    Caller identity is ALWAYS derived from the authenticated credential --
    never from any request field. Callers with an unresolved (empty) token
    can never match, even against an empty presented token.

    If more than one caller matches (a duplicate-credential config), this
    returns None -- authenticates as nobody -- rather than picking one, e.g.
    the last match found while iterating. ``load_inbound_config`` already
    rejects duplicate-credential configs at load time (the preferred,
    deterministic fail-closed point); this is defense in depth so a
    duplicate can never silently resolve to, and inherit the route of,
    whichever caller happens to be matched.
    """
    if not presented_token:
        return None
    matches: list = []
    # Compare against every configured caller (not just until first match) so
    # the number of configured callers doesn't leak via early-return timing,
    # and so a duplicate credential is reliably detected as >1 match rather
    # than depending on dict iteration order.
    for caller_id, rule in inbound.callers.items():
        if not rule.token:
            continue
        try:
            if hmac.compare_digest(presented_token.encode(), rule.token.encode()):
                matches.append(caller_id)
        except (UnicodeEncodeError, TypeError):
            continue
    if len(matches) != 1:
        return None
    return matches[0]


def is_ask_authorized(inbound: InboundConfig, caller_id: str) -> bool:
    """Deny-by-default caller -> target -> ask check."""
    rule = inbound.callers.get(caller_id)
    if rule is None:
        return False
    return inbound.self_target in rule.allow_targets


def safe_redact(text: Optional[str]) -> Optional[str]:
    """Redact *text*, failing CLOSED: returns None if redaction itself raises.

    Callers must treat ``None`` as "cannot safely emit this text" and fall
    back to a generic message/error -- never pass the raw text through.
    """
    if text is None:
        return None
    try:
        return redact_sensitive_text(text, force=True)
    except Exception:
        logger.exception("persona_api: redaction failed; failing closed")
        return None


def new_correlation_id() -> str:
    """Server-generated correlation id. Never accepted from a caller-supplied
    field -- a client-controlled correlation id would be an easy injection
    vector into audit logs for no operational benefit."""
    return uuid.uuid4().hex


def build_receipt(
    *,
    status: str,
    target: str,
    correlation_id: str,
    answer_text: Optional[str] = None,
    error_code: Optional[str] = None,
    message: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the closed-schema ask receipt: status/target/correlation_id/answer_text
    plus a typed error_code/message pair. No raw logs, tool output, or traceback
    ever belongs in this envelope -- every caller of this function must have
    already reduced any failure to a short, safe, generic string.
    """
    return {
        "object": "hermes.persona.ask_receipt",
        "status": status,
        "target": target,
        "correlation_id": correlation_id,
        "answer_text": answer_text,
        "error_code": error_code,
        "message": message,
    }


def audit_log(
    event: str,
    *,
    caller_id: str = "",
    target_id: str = "",
    correlation_id: str = "",
    outcome: str = "",
    detail: Optional[str] = None,
) -> None:
    """Structured, secret-free audit log line for a persona ask event.

    Only identifiers/outcome are logged by default. ``detail`` (if given) is
    redacted fail-closed: if redaction raises, the detail is replaced with a
    fixed placeholder rather than risk emitting raw content -- the audit
    event itself is still logged either way.
    """
    safe_detail = "[unavailable]"
    if detail is not None:
        redacted = safe_redact(detail)
        safe_detail = redacted if redacted is not None else "[redaction-failed]"
    logger.info(
        "persona_api event=%s caller=%s target=%s correlation_id=%s outcome=%s detail=%s",
        event, caller_id or "-", target_id or "-", correlation_id or "-", outcome or "-", safe_detail,
    )
