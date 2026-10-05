"""Offline synthetic SDK fixtures; no provider quota API, real credentials or canary.

Message wording is from Kimi Code's public error reference/FAQ:
https://www.kimi.com/code/docs/en/kimi-code/error-reference.html
https://www.kimi.com/code/docs/en/kimi-code/faq.html
Reset values, SDK envelopes and accounts below are deliberately synthetic.
"""
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock
import socket
import time

import anthropic
import httpx
import openai
import pytest

from agent.agent_runtime_helpers import extract_api_error_context, recover_with_credential_pool
from agent.credential_pool import CredentialPool, PooledCredential, STATUS_EXHAUSTED
from agent.error_classifier import FailoverReason, classify_api_error
from agent.turn_api_error import handle_api_error
from agent.turn_recovery import compute_error_backoff
from agent.turn_retry_state import TurnRetryState

BASE = "https://api.kimi.ai/coding/v1"
QUOTAS = {
    "5-hour": "You've reached your 5-hour usage limit. Your quota will reset when the current 5-hour window ends. To continue now, purchase extra usage or upgrade your plan: https://www.kimi.com/membership/subscription?tab=quota",
    "weekly": "You've reached your weekly (7-day) usage limit. Your quota will reset when the current window ends.",
    "monthly": "You've reached your monthly usage limit for this billing cycle.",
}
CONCURRENT = "You've reached your concurrent request limit. Please wait for your ongoing requests to finish and try again."


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Offline test attempted network access")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    # Pool persistence may only target the per-test home; keep this suite purely
    # in-memory (no auth-store fixtures even in the isolated home).
    monkeypatch.setattr(CredentialPool, "_persist", lambda self, **kwargs: None)
    monkeypatch.setattr("agent.credential_pool._iter_custom_providers", lambda: [
        ("kimi-api", {"name": "kimi-api", "provider_key": "kimi-api", "base_url": BASE})
    ])


def sdk_error(message, *, status=403, nested=True, sdk="openai", fields=None, headers=None):
    payload = {"message": message, **(fields or {})}
    body = {"error": payload} if nested else payload
    response = httpx.Response(status, request=httpx.Request("POST", BASE + "/chat/completions"), headers=headers)
    module = openai if sdk == "openai" else anthropic
    cls = module.PermissionDeniedError if status == 403 else module.RateLimitError if status == 429 else module.AuthenticationError
    return cls("Synthetic SDK status error", response=response, body=body)


def classify(error, base=BASE, provider="custom:kimi-api"):
    return classify_api_error(error, provider=provider, model="k3-256k", base_url=base)


@pytest.mark.parametrize("sdk", ["openai", "anthropic"])
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("status", [403, 429])
@pytest.mark.parametrize("period", list(QUOTAS))
def test_documented_periodic_caps_are_not_auth_or_billing(sdk, nested, status, period):
    ce = classify(sdk_error(QUOTAS[period], sdk=sdk, nested=nested, status=status))
    assert ce.reason is FailoverReason.rate_limit and not ce.is_auth
    assert not ce.retryable and ce.should_rotate_credential and ce.should_fallback
    assert ce.error_context["quota_exhausted"] and ce.error_context["usage_limit_reached"]
    assert ce.error_context["quota_period"] == period
    assert ce.error_context["reset_unknown"] and ce.error_context["retry_probe_after"] == 3600
    assert "reset_at" not in ce.error_context


@pytest.mark.parametrize("host", ["api.kimi.ai", "api.kimi.com"])
@pytest.mark.parametrize("path", ["/coding", "/coding/", "/coding/v1", "/coding/v1/"])
def test_official_hosts_and_protocol_roots(host, path):
    base = f"https://{host}{path}"
    assert classify(sdk_error(QUOTAS["weekly"]), base, "custom").error_context["quota_exhausted"]
    assert base == f"https://{host}{path}"  # literal route was not rewritten


@pytest.mark.parametrize("base", [
    "", "https://api.moonshot.ai/v1", "https://api.moonshot.cn/v1",
    "https://api.kimi.ai.evil.test/coding/v1", "https://evilapi.kimi.ai/coding/v1",
    "https://evil.test/api.kimi.ai/coding/v1", "https://api.kimi.ai@evil.test/coding/v1",
    "https://api.kimi.ai/v1", "https://api.kimi.ai/coding-impostor/v1",
    "https://api.kimi.ai/other/coding/v1", "https://api.kimi.ai/coding/../v1",
    "https://[invalid/coding/v1", "https://evil.test/?next=https://api.kimi.ai/coding/v1",
])
@pytest.mark.parametrize("status", [403, 429])
def test_negative_scope_does_not_override_403(base, status):
    ce = classify(sdk_error(QUOTAS["monthly"], status=status), base)
    assert ce.reason is (FailoverReason.auth if status == 403 else FailoverReason.billing)
    assert not ce.error_context.get("quota_exhausted")


def test_401_never_gets_periodic_quota_exception():
    assert classify(sdk_error(QUOTAS["monthly"], status=401)).is_auth


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("message,code", [("Invalid Authentication", "authentication_error"), ("Invalid API key", "invalid_api_key"), ("Forbidden", "permission_denied")])
def test_genuine_auth_is_preserved(status, message, code):
    assert classify(sdk_error(message, status=status, fields={"code": code})).is_auth


@pytest.mark.parametrize("message,code,reason", [
    ("Your request was blocked. Attention required! | Cloudflare", "", FailoverReason.upstream_blocked),
    ("Insufficient credits", "insufficient_credits", FailoverReason.billing),
    ("Subscription expired", "", FailoverReason.auth),
    ("Monthly budget limit exceeded", "", FailoverReason.billing),
    ("Invalid API key. " + QUOTAS["weekly"], "invalid_api_key", FailoverReason.auth),
    ("Request blocked. " + QUOTAS["weekly"], "", FailoverReason.upstream_blocked),
])
def test_other_refusals_and_quota_mentions_keep_priority(message, code, reason):
    assert classify(sdk_error(message, fields={"code": code})).reason is reason


@pytest.mark.parametrize("code", ["usage_limit_reached", "quota_exhausted"])
@pytest.mark.parametrize("nested", [False, True])
def test_scoped_structured_quota_without_period(code, nested):
    ce = classify(sdk_error("Limit reached", fields={"code": code}, nested=nested))
    assert ce.error_context["quota_exhausted"] and ce.error_context["reset_unknown"]


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("sdk", ["openai", "anthropic"])
@pytest.mark.parametrize("status", [403, 429])
def test_concurrency_is_distinct_and_retryable(nested, sdk, status):
    ce = classify(sdk_error(CONCURRENT, nested=nested, sdk=sdk, status=status))
    assert ce.reason is FailoverReason.rate_limit and ce.retryable and not ce.is_auth
    assert ce.error_context == {"limit_kind": "concurrency"}
    assert not ce.should_rotate_credential and ce.should_fallback


@pytest.mark.parametrize("reset_form", ["epoch", "epoch-ms", "epoch-string", "iso", "relative", "body-retry", "retry-header", "http-date", "x-reset", "vendor-duration", "vendor-iso", "message"])
@pytest.mark.parametrize("nested", [False, True])
def test_reliable_reset_grammars(reset_form, nested):
    reset = float(int(time.time()) + 7200)
    fields, headers, message = {}, {}, QUOTAS["weekly"]
    if reset_form == "epoch": fields["resets_at"] = reset
    elif reset_form == "epoch-ms": fields["reset_at"] = reset * 1000
    elif reset_form == "epoch-string": fields["reset_at"] = str(reset)
    elif reset_form == "iso": fields["reset_at"] = datetime.fromtimestamp(reset, timezone.utc).isoformat()
    elif reset_form == "relative": fields["resets_in_seconds"] = 7200
    elif reset_form == "body-retry": fields["retry_after"] = "7200"
    elif reset_form == "retry-header": headers["Retry-After"] = "7200"
    elif reset_form == "http-date": headers["Retry-After"] = datetime.fromtimestamp(reset, timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    elif reset_form == "x-reset": headers["x-ratelimit-reset"] = str(reset)
    elif reset_form == "vendor-duration": headers["x-ratelimit-reset-requests"] = "2h0m0s"
    elif reset_form == "vendor-iso": headers["anthropic-ratelimit-tokens-reset"] = datetime.fromtimestamp(reset, timezone.utc).isoformat()
    else: message += " Resets in 2 hours."
    ce = classify(sdk_error(message, fields=fields, headers=headers, nested=nested))
    assert ce.error_context["reset_at"] == pytest.approx(reset, abs=2)
    assert "reset_unknown" not in ce.error_context


def test_explicit_reset_wins_over_short_retry_header():
    reset = time.time() + 86400
    ce = classify(sdk_error(QUOTAS["monthly"], fields={"reset_at": reset}, headers={"Retry-After": "2"}))
    assert ce.error_context["reset_at"] == reset


@pytest.mark.parametrize("bad", [True, "garbage", float("nan"), float("inf"), 5, "5"])
def test_invalid_or_expired_reset_does_not_become_relative_wait(bad):
    ce = classify(sdk_error(QUOTAS["5-hour"], fields={"reset_at": bad}))
    assert ce.error_context["reset_unknown"] and "reset_at" not in ce.error_context


class Agent:
    provider = "custom:kimi-api"
    requested_provider = "custom:kimi-api"
    model = "k3-256k"
    base_url = BASE
    api_mode = "chat_completions"
    log_prefix = ""
    verbose_logging = False
    quiet_mode = True
    thinking_callback = None
    _interrupt_requested = False
    _image_rejecting_models = set()
    _fallback_index = 0
    _fallback_chain: tuple[dict[str, Any], ...] = ()
    _credential_pool_revert_id = None
    _credential_pool_entry_id = None
    context_compressor = None

    def __init__(self, pool=None):
        self._credential_pool = pool
        self.api_key = "synthetic-0"
        self.calls = []
        self._try_activate_fallback = MagicMock(return_value=False)
        self._try_recover_primary_transport = MagicMock(return_value=False)
        self._try_refresh_nous_client_credentials = MagicMock(return_value=False)
        self._summarize_api_error = lambda error: "Synthetic quota/concurrency refusal"
        self._has_pending_fallback = lambda: bool(self._fallback_chain)
        for name in ["_invoke_api_request_error_hook", "_touch_activity", "_buffer_vprint", "_buffer_diagnostic_status", "_emit_diagnostic_status", "_emit_diagnostic_wait", "_flush_status_buffer", "_persist_session", "_dump_api_request_debug", "_vprint"]:
            setattr(self, name, MagicMock())

    def _extract_api_error_context(self, error):
        return extract_api_error_context(error)

    def _recover_with_credential_pool(self, **kwargs):
        self.calls.append(kwargs)
        return recover_with_credential_pool(self, **kwargs)

    def _swap_credential(self, entry):
        self.api_key = entry.runtime_api_key
        self._credential_pool_entry_id = entry.id
        return True

    def _client_log_context(self): return "synthetic offline agent"
    def _clean_error_message(self, text): return text
    def _is_openrouter_url(self): return False


def entry(i, provider="custom:kimi-api", base=BASE):
    return PooledCredential(provider=provider, id=f"synthetic-{i}", label=f"fixture-{i}", auth_type="api_key", priority=i, source="manual", access_token=f"synthetic-{i}", base_url=base)


def run_error(agent, error, retry=None, max_retries=3, retry_count=0) -> Any:
    return handle_api_error(
        agent, api_error=error, _retry=retry or TurnRetryState(), thinking_spinner=None,
        messages=[], api_messages=[], api_kwargs=None, system_message=None,
        active_system_prompt="fixture", conversation_history=[], approx_tokens=1,
        retry_count=retry_count, max_retries=max_retries, compression_attempts=0,
        max_compression_attempts=3, api_call_count=1, api_request_id="offline-r",
        api_start_time=time.time(), effective_task_id="fixture", turn_id="fixture-turn",
    )


def test_fake_pool_receives_semantics_before_any_retry_or_refresh():
    pool = MagicMock(provider="custom:kimi-api")
    pool.provider = "custom:kimi-api"
    pool.entries.return_value = [entry(0)]
    pool.current.return_value = entry(0)
    pool.mark_exhausted_and_rotate.return_value = None
    agent = Agent(pool)
    result = run_error(agent, sdk_error(QUOTAS["weekly"]))
    assert result.action == "return"
    pool.mark_exhausted_and_rotate.assert_called_once()
    kwargs = pool.mark_exhausted_and_rotate.call_args.kwargs
    assert kwargs["status_code"] == 403 and kwargs["failure_reason"] == "quota_exhausted"
    assert kwargs["error_context"]["reset_unknown"] and kwargs["error_context"]["usage_limit_reached"]
    pool.try_refresh_matching.assert_not_called()
    agent._try_refresh_nous_client_credentials.assert_not_called()
    agent._try_activate_fallback.assert_called_once_with(reason=FailoverReason.rate_limit, reset_at=None)
    assert result.result["failure_reason"] == "rate_limit" and not result.result["failure_retryable"]
    assert result.result["reset_unknown"] and result.result["retry_probe_after"] == 3600
    assert "provider reset" in result.result["final_response"]
    assert "failure_resets_at" not in result.result


@pytest.mark.parametrize("reset_known", [False, True])
def test_real_in_memory_pool_rotation_and_sole_quota_ttl(reset_known):
    pool = CredentialPool("custom:kimi-api", [entry(0), entry(1)])
    pool.try_refresh_matching = MagicMock(side_effect=AssertionError("quota must not refresh"))
    agent = Agent(pool)
    reset = time.time() + 86400
    fields = {"reset_at": reset} if reset_known else {}
    error = sdk_error(QUOTAS["monthly"], fields=fields)
    verdict = run_error(agent, error)
    assert verdict.action == "continue" and agent.api_key == "synthetic-1"
    agent._try_activate_fallback.assert_not_called()
    verdict = run_error(agent, error)
    assert verdict.action == "return" and pool.select() is None
    assert all(e.last_status == STATUS_EXHAUSTED and e.failure_reason == "quota_exhausted" for e in pool.entries())
    probe_at = pool.next_available_at()
    assert probe_at is not None
    wait = probe_at - time.time()
    assert wait > (86000 if reset_known else 3500)
    assert verdict.result["quota_exhausted"] and verdict.result["quota_period"] == "monthly"
    if reset_known:
        assert verdict.result["failure_resets_at"] == reset
        assert agent._try_activate_fallback.call_args.kwargs["reset_at"] == reset
    else:
        assert "failure_resets_at" not in verdict.result
    # One remaining credential must not be retried after the transient60s TTL.
    sole = CredentialPool("custom:kimi-api", [entry(0)])
    assert run_error(Agent(sole), error).action == "return"
    sole_probe_at = sole.next_available_at()
    assert sole_probe_at is not None and sole_probe_at - time.time() > 3500
    pool.try_refresh_matching.assert_not_called()


def test_semantic_reset_propagation_over_raw_short_hint():
    reset = time.time() + 86400
    pool = CredentialPool("custom:kimi-api", [entry(0)])
    agent = Agent(pool)
    agent._extract_api_error_context = lambda error: {"message": "raw", "reset_at": time.time() + 2, "extra": "preserved"}
    verdict = run_error(agent, sdk_error(QUOTAS["weekly"], fields={"reset_at": reset}))
    assert agent.calls[0]["error_context"]["extra"] == "preserved"
    assert pool.next_available_at() == reset
    assert verdict.result["failure_resets_at"] == reset


def test_generic_iso_reset_context_remains_safe_at_terminal():
    # Merging raw and classified context is shared machinery: preserve legacy
    # non-Kimi reset strings without subtracting a string from the clock.
    agent = Agent()
    agent.provider, agent.base_url, agent.model = "openai", "https://api.openai.com/v1", "synthetic-model"
    reset = float(int(time.time()) + 7200)
    iso = datetime.fromtimestamp(reset, timezone.utc).isoformat()
    error = sdk_error("Rate limit exceeded", status=429, fields={"reset_at": iso})
    verdict = run_error(agent, error, retry_count=2)
    assert verdict.action == "return" and verdict.result["failure_resets_at"] == reset


def test_no_raw_reset_resurrection_after_classifier_rejects_reset():
    agent = Agent()
    verdict = run_error(agent, sdk_error(QUOTAS["5-hour"], fields={"reset_at": 5}))
    assert verdict.action == "return" and "failure_resets_at" not in verdict.result
    assert "reset_at" not in agent.calls[0]["error_context"]


def test_periodic_quota_activates_native_fallback_once(monkeypatch):
    import agent.conversation_loop as loop
    arm = MagicMock(return_value="rebuilt")
    monkeypatch.setattr(loop, "_arm_fallback_restart", arm)
    agent = Agent()
    agent._fallback_chain = ({"provider": "openai-codex", "model": "synthetic-codex"},)
    agent._try_activate_fallback.return_value = True
    verdict = run_error(agent, sdk_error(QUOTAS["weekly"]))
    assert verdict.action == "break" and verdict.active_system_prompt == "rebuilt"
    agent._try_activate_fallback.assert_called_once_with(reason=FailoverReason.rate_limit, reset_at=None)
    arm.assert_called_once()


def test_concurrency_has_bounded_short_wait_and_no_pool_bench(monkeypatch):
    import agent.turn_api_error as api
    sleep = MagicMock(return_value=None)
    monkeypatch.setattr(api, "interruptible_backoff_sleep", sleep)
    pool = CredentialPool("custom:kimi-api", [entry(0)])
    pool.mark_exhausted_and_rotate = MagicMock(side_effect=AssertionError("concurrency must not bench"))
    pool.try_refresh_matching = MagicMock(side_effect=AssertionError("concurrency must not refresh"))
    agent = Agent(pool)
    error = sdk_error(CONCURRENT, headers={"Retry-After": "2592000"}, fields={"reset_at": time.time() + 2592000})
    retry = TurnRetryState()
    for i in range(2):
        verdict = run_error(agent, error, retry, max_retries=100, retry_count=i)
        assert verdict.action == "fallthrough" and verdict.max_retries == 3
        assert sleep.call_args.args[1] <= 8
        assert "reset_at" not in agent.calls[-1]["error_context"]
    verdict = run_error(agent, error, retry, max_retries=100, retry_count=2)
    assert verdict.action == "return" and sleep.call_count == 2
    assert verdict.result["failure_limit_kind"] == "concurrency"
    assert "failure_resets_at" not in verdict.result
    assert pool.next_available_at() is None
    pool.mark_exhausted_and_rotate.assert_not_called()
    pool.try_refresh_matching.assert_not_called()


@pytest.mark.real_retry_backoff
def test_concurrency_backoff_never_uses_long_reset():
    agent = Agent()
    wait = compute_error_backoff(agent, sdk_error(CONCURRENT, headers={"Retry-After": "2592000"}), retry_count=999,
        max_retries=1000, is_rate_limited=True, is_zai_coding_overload=False, base_url=BASE, model=agent.model,
        error_context={"limit_kind": "concurrency"})
    assert 0 < wait <= 8


def test_both_provider_pools_exhausted_offline(monkeypatch):
    """Synthetic Codex quota then Kimi quota; only native error recovery/fallback contract is invoked."""
    import agent.conversation_loop as loop
    monkeypatch.setattr(loop, "_arm_fallback_restart", lambda agent, msgs, prompt, retry: prompt)
    codex_base = "https://chatgpt.com/backend-api/codex"
    codex_pool = CredentialPool("openai-codex", [entry(0, "openai-codex", codex_base)])
    kimi_pool = CredentialPool("custom:kimi-api", [entry(0)])
    agent = Agent(codex_pool)
    agent.provider, agent.base_url, agent.model = "openai-codex", codex_base, "synthetic-codex"
    agent._fallback_chain = ({"provider": "custom:kimi-api", "model": "k3-256k"},)
    agent._fallback_index = 0
    transitions = []
    def activate(reason=None, reset_at=None):
        if agent._fallback_index:
            return False
        transitions.append(reason)
        agent._fallback_index = 1
        agent.provider, agent.base_url, agent.model = "custom:kimi-api", BASE, "k3-256k"
        agent._credential_pool = kimi_pool
        return True
    agent._try_activate_fallback = MagicMock(side_effect=activate)
    ce = sdk_error("You've reached your usage limit.", status=429, fields={"code": "usage_limit_reached", "resets_in_seconds": 7200})
    verdict = run_error(agent, ce)
    assert verdict.action == "break" and codex_pool.select() is None
    verdict = run_error(agent, sdk_error(QUOTAS["monthly"]))
    assert verdict.action == "return" and kimi_pool.select() is None
    assert transitions == [FailoverReason.rate_limit]
    assert verdict.result["failure_reason"] == "rate_limit" and verdict.result["reset_unknown"]
