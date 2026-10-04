"""Rodolfo/Moss ingress boundary. Opt-in only; no lifecycle/guard changes.

Installed as agent.moss_memory_gate in the reviewed image. Secrets and policy
are external runtime inputs, never embedded in this source.
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
import functools
import hashlib
import hmac
import json
import os
import re
import stat
import threading
import time
from pathlib import Path

POLICY = Path('/opt/data/moss-memory-policy.json')
KEY = Path('/opt/data/moss-memory-gate.key')
HEADER = 'X-Moss-Memory-Proof'
api_admission: contextvars.ContextVar[dict | None] = contextvars.ContextVar('moss_memory_api_admission', default=None)
web_admission: contextvars.ContextVar[dict | None] = contextvars.ContextVar('moss_memory_web_admission', default=None)
_seen: dict[str, float] = {}
_lock = threading.Lock()


def moss_runtime():
    """Distribution selector only, never proof of a human conversation.

    The recovery overlay was Moss-only; this fork image serves six personas.
    Keep hooks exclusive to that image role, while eligible() still requires
    the external policy, owner/home and cryptographic admission.
    """
    return os.environ.get('AGENT_NAME') == 'moss'


@contextmanager
def admission_scope(admission):
    token = api_admission.set(admission)
    try:
        yield
    finally:
        api_admission.reset(token)


def _root_read(path, forbidden_mode):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_mode & forbidden_mode:
            raise ValueError('Untrusted memory policy or key ownership')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            return stream.read()
    finally:
        os.close(fd)


def policy():
    if not POLICY.exists():
        return None
    p = json.loads(_root_read(POLICY, 0o022))
    if p.get('version') != 1 or p.get('workspace') != 'moss-rodolfo':
        raise ValueError('Unsupported Moss memory policy')
    return p


def _key():
    value = _root_read(KEY, 0o027)
    if len(value) != 32:
        raise ValueError('Invalid memory proof key custody')
    return value


def _mac(value):
    return hmac.new(_key(), value.encode(), hashlib.sha256).hexdigest()


def capture_browser(fn):
    """Only the actual cookie-authenticated chat/start handler may mint admission.

    API keys, trusted headers, service launches and server wakeups do not qualify.
    Password login maps to the single owner only by explicit runtime policy.
    """
    @functools.wraps(fn)
    def wrapped(handler, body, *args, **kwargs):
        admission = None
        p = policy()
        if p:
            from api.auth import get_session_info, parse_cookie
            from api.profiles import get_active_profile_name
            info = get_session_info(parse_cookie(handler) or '')
            profile = get_active_profile_name() or 'default'
            # Old valid cookies authorize chat, but cannot prove password login.
            # Do not silently run a human memory test without its memory provider.
            if (info and not info.get('auth_type') and profile in p['profiles']
                    and p.get('password_owner') == 'Rodolfo'):
                from api.routes import j
                return j(handler, {
                    'error': 'Honcho exige novo login por senha. Saia do WebUI, entre novamente e reenvie a mensagem; ela não foi executada.',
                    'code': 'honcho_password_reauthentication_required',
                    'memory_admitted': False,
                }, status=409)
            if (info and info.get('auth_type') == 'password'
                    and p.get('password_owner') == 'Rodolfo'
                    and profile in p['profiles']
                    and info.get('bound_profile') in (None, '', profile)):
                admission = {'profile': profile, 'expires': min(float(info['expiry']), time.time() + 120)}
        token = web_admission.set(admission)
        try:
            return fn(handler, body, *args, **kwargs)
        finally:
            web_admission.reset(token)
    return wrapped


def sign_request(admission, body: bytes, session_id: str, profile: str):
    """Bind the entire request, profile and declared conversation, not its label."""
    if not admission or admission.get('profile') != profile or admission.get('expires', 0) < time.time():
        return {}
    import secrets
    now = int(time.time())
    nonce = secrets.token_hex(16)
    payload = f'{now}:{nonce}:{profile}:{session_id}:{hashlib.sha256(body).hexdigest()}'
    return {HEADER: f'{now}.{nonce}.{_mac(payload)}'}


def verify_request(header: str, body: bytes, session_id: str, profile: str, *, now=None):
    p = policy()
    if not p or profile not in p['profiles'] or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', session_id):
        return None
    if not re.fullmatch(r'[0-9]{10}\.[a-f0-9]{32}\.[a-f0-9]{64}', header or ''):
        return None
    ts, nonce, signature = header.split('.')
    now = time.time() if now is None else now
    if not -5 <= now - int(ts) <= 120:
        return None
    payload = f'{ts}:{nonce}:{profile}:{session_id}:{hashlib.sha256(body).hexdigest()}'
    if not hmac.compare_digest(_mac(payload), signature):
        return None
    with _lock:
        for k, expiry in list(_seen.items()):
            if expiry < now:
                del _seen[k]
        if nonce in _seen or len(_seen) >= 10000:
            return None
        _seen[nonce] = now + 125
    return {'session_id': session_id, 'profile': profile, 'principal': 'Rodolfo', 'surface': 'webui'}


async def request_admission(request, profile):
    """Called inside profile scope. Ordinary API calls remain memory-ineligible."""
    if request.method != 'POST' or request.path.rstrip('/') not in ('/v1/runs', '/p/moss/v1/runs', '/p/default/v1/runs'):
        return None
    proof = request.headers.get(HEADER, '')
    if not proof:
        return None
    body = await request.read()
    if len(body) > 2_000_000:
        return None
    admission = verify_request(proof, body, request.headers.get('X-Hermes-Session-Id', ''), profile)
    if admission:
        data = json.loads(body)
        if data.get('session_id') != admission['session_id'] or data.get('profile', profile) != profile:
            return None
        if request.headers.get('X-Hermes-Session-Key') != 'webui:' + admission['session_id']:
            return None
        if data.get('turn_author') or data.get('room_dispatch') or data.get('relay_metadata'):
            return None
    return admission


def eligible(kwargs):
    p = policy()
    if not p:
        return False  # This overlay is exclusive to Moss; missing policy never authorizes memory.
    from hermes_constants import get_hermes_home
    if str(get_hermes_home()) not in p['homes'] or kwargs.get('agent_context') != 'primary':
        return False
    platform = kwargs.get('platform')
    if platform == 'telegram':
        owner = p.get('telegram_owner_id')
        return bool(owner and str(kwargs.get('user_id')) == owner
                    and str(kwargs.get('chat_id')) == owner and kwargs.get('chat_type') == 'dm')
    if platform == 'api_server':
        admission = api_admission.get()
        return bool(admission and admission.get('principal') == 'Rodolfo'
                    and (admission.get('session_id') == kwargs.get('session_id')
                         or kwargs.get('gateway_session_key') == 'webui:' + admission['session_id']))
    return False


def scrub(value):
    """Drop the whole string when any credential is detected; no partial key remnants.

    A detector is not a universal secret oracle. Known credential formats,
    assignments, private keys, URL credentials and registered vault values are
    handled by the runtime detector; unknown unlabeled strings remain a limitation.
    """
    from agent.redact import redact_sensitive_text
    if isinstance(value, str):
        # Outbound memory is stricter than upstream's low-entropy log heuristic.
        assignments = re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*[:=]\s*([^\s&,;]+)", value)
        for match in assignments:
            name, assigned = match.groups()
            if name.lower() == 'token_budget' and re.fullmatch(r'[0-9]{1,9}', assigned):
                continue
            if any(word in name.lower() for word in ('token', 'password', 'secret', 'api_key')):
                return '[content omitted: credential detected]'
        clean = redact_sensitive_text(value, force=True, redact_url_credentials=True)
        return '[content omitted: credential detected]' if clean != value else value
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items()}
    return value


def safe_http_client(base_url, timeout, *, api_key='local'):
    import httpx
    from urllib.parse import urlsplit
    expected = urlsplit(base_url)
    credential_name = re.compile(r'^(?:x[-_])?(?:api[-_]?key|access[-_]?token|refresh[-_]?token|token|password|passwd|secret|client[-_]?secret|credential|auth|key)$', re.I)
    def outbound(request):
        if (request.url.scheme, request.url.host, request.url.port) != (expected.scheme, expected.hostname, expected.port or (443 if expected.scheme == 'https' else 80)):
            raise ValueError('Honcho destination drift')
        if request.url.userinfo or scrub(request.url.path) != request.url.path:
            raise ValueError('Credential-bearing Honcho URL refused')
        for name, value in request.url.params.multi_items():
            if credential_name.fullmatch(name) or scrub(name + '=' + value) != name + '=' + value:
                raise ValueError('Credential-bearing Honcho query refused')
        for name, value in request.headers.multi_items():
            if name.lower() == 'authorization':
                # The only allowed authentication value for this observed,
                # unauthenticated self-hosted deployment is the SDK placeholder.
                if api_key != 'local' or value != 'Bearer local':
                    raise ValueError('Unexpected Honcho authentication credential')
            elif (name.lower() in ('cookie','set-cookie','proxy-authorization')
                  or credential_name.fullmatch(name)
                  or scrub(value) != value):
                raise ValueError('Credential-bearing Honcho header refused')
        if request.content:
            if 'application/json' not in request.headers.get('content-type', ''):
                raise ValueError('Non-JSON Honcho payload refused')
            raw = json.loads(request.content)
            sanitized = json.dumps(scrub(raw), ensure_ascii=False).encode()
            request._content = sanitized
            request.stream = httpx.ByteStream(sanitized)
            request.headers['Content-Length'] = str(len(sanitized))
    return httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False,
                        event_hooks={'request': [outbound]})


def read_only_status(hcfg):
    """Moss diagnostic must not get-or-create a conversation or refresh OAuth."""
    if not moss_runtime():
        return False
    approved = policy()
    if (not approved or hcfg.workspace_id != approved['workspace']
            or hcfg.base_url != approved['base_url']):
        raise ValueError('Moss Honcho status destination mismatch')
    with safe_http_client(hcfg.base_url, 10) as client:
        response = client.post(hcfg.base_url + '/v3/workspaces/list?page=1&size=100', json={})
        response.raise_for_status()
        data = response.json()
        if data.get('total', 0) > len(data.get('items', [])):
            raise ValueError('Workspace inventory incomplete')
        if not any(w['id'] == hcfg.workspace_id for w in data['items']):
            raise ValueError('Configured workspace missing')
    print('\n  Connection: reachable (read-only; no session created).')
    print('  Channel ingestion: NOT VERIFIED. Cross-session recall: NOT VERIFIED.\n')
    return True
