import asyncio
import importlib.util
import json
import sys
import time
import types
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('candidate_gate', ROOT / 'agent/moss_memory_gate.py')
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    p = {'version': 1, 'workspace': 'moss-rodolfo', 'profiles': ['default', 'moss'],
         'homes': ['/opt/data', '/opt/data/profiles/moss'], 'telegram_owner_id': '12345',
         'password_owner': 'Rodolfo', 'base_url': 'http://honcho-api:8000'}
    monkeypatch.setattr(gate, 'policy', lambda: p)
    monkeypatch.setattr(gate, '_key', lambda: b'unit-test-key-not-a-real-secret!!')
    monkeypatch.setattr('hermes_constants.get_hermes_home', lambda: Path('/opt/data'))
    gate._seen.clear()
    at, wt = gate.api_admission.set(None), gate.web_admission.set(None)
    yield p
    gate.api_admission.reset(at)
    gate.web_admission.reset(wt)


def proof(body=b'{"session_id":"s1"}', profile='default', sid='s1'):
    return gate.sign_request({'profile': profile, 'expires': time.time() + 60}, body, sid, profile)[gate.HEADER]


def test_signature_bound_and_single_use():
    body = b'{"session_id":"s1"}'
    signed = proof(body)
    assert gate.verify_request(signed, body+b' ', 's1', 'default') is None
    assert gate.verify_request(signed, body, 's2', 'default') is None
    assert gate.verify_request(signed, body, 's1', 'moss') is None
    assert gate.verify_request(signed, body, 's1', 'default')['principal'] == 'Rodolfo'
    assert gate.verify_request(signed, body, 's1', 'default') is None


@pytest.mark.parametrize('header', ['', 'forged', '0.'+'a'*32+'.'+'b'*64, '9'*20000])
def test_invalid_headers(header):
    assert gate.verify_request(header,b'{}','s1','default') is None


def test_expiry_and_future():
    header = proof()
    assert gate.verify_request(header,b'{"session_id":"s1"}','s1','default',now=time.time()+121) is None
    assert gate.verify_request(header,b'{"session_id":"s1"}','s1','default',now=time.time()-20) is None


def test_no_browser_admission():
    assert gate.sign_request(None,b'{}','s1','default') == {}
    assert gate.sign_request({'profile':'moss','expires':time.time()+30},b'{}','s1','default') == {}
    assert gate.sign_request({'profile':'default','expires':0},b'{}','s1','default') == {}


@pytest.mark.parametrize('platform', ['cron','webhook','cli','webui','api_server','subagent','a2a','telegram'])
def test_surface_label_is_not_auth(platform):
    assert not gate.eligible({'agent_context':'primary','platform':platform,'session_id':'s1'})


def test_telegram_owner_dm_only():
    k = {'agent_context':'primary','platform':'telegram','user_id':'12345','chat_id':'12345','chat_type':'dm'}
    assert gate.eligible(k)
    for field, bad in [('chat_type','group'),('user_id','other'),('chat_id','-12345'),('agent_context','cron')]:
        assert not gate.eligible({**k,field:bad})


def test_verified_browser_session_only():
    gate.api_admission.set({'principal':'Rodolfo','session_id':'s1','profile':'default'})
    assert gate.eligible({'platform':'api_server','session_id':'s1','agent_context':'primary'})
    assert not gate.eligible({'platform':'api_server','session_id':'s2','agent_context':'primary'})
    assert not gate.eligible({'platform':'cron','session_id':'s1','agent_context':'primary'})


def test_no_policy_is_fail_closed(monkeypatch):
    monkeypatch.setattr(gate,'policy',lambda:None)
    assert not gate.eligible({'platform':'telegram','user_id':'12345','chat_id':'12345','chat_type':'dm','agent_context':'primary'})


def test_auth_cookie_required(monkeypatch):
    info = {'auth_type':'password','expiry':time.time()+60,'bound_profile':None}
    monkeypatch.setitem(sys.modules,'api.auth',types.SimpleNamespace(get_session_info=lambda _:info,parse_cookie=lambda _: 'test-cookie'))
    monkeypatch.setitem(sys.modules,'api.profiles',types.SimpleNamespace(get_active_profile_name=lambda:'default'))
    @gate.capture_browser
    def handler(h,b):
        return gate.web_admission.get()
    assert handler(None,{})['profile'] == 'default'
    assert gate.web_admission.get() is None
    for kind in ['trusted','oidc','passkey']:
        info['auth_type'] = kind
        assert handler(None,{}) is None
    info['auth_type']='password'
    info['bound_profile']='roy'
    assert handler(None,{}) is None


def test_async_context_captured_not_global():
    async def run():
        token=gate.api_admission.set({'principal':'Rodolfo'})
        task=asyncio.create_task(async_read())
        gate.api_admission.reset(token)
        assert gate.api_admission.get() is None
        assert await task == {'principal':'Rodolfo'}
    async def async_read():
        return gate.api_admission.get()
    asyncio.run(run())


@pytest.mark.parametrize('secret', [
    'api_key = "sk-proj-abcdefghijklmnopqrstuvxyz1234567890"',
    'Authorization: Bearer abcdefghijklmnopqrstuvxyz1234567890',
    'postgresql://someone:very-secret-password@db.example/postgres',
    '-----BEGIN PRIVATE KEY-----\nabcdefabcdef\n-----END PRIVATE KEY-----',
    'https://example.test/path?api_key=abcdefghijklmnopqrstuvxyz1234567890',
])
def test_credential_payload_removed(secret):
    assert gate.scrub(secret) == '[content omitted: credential detected]'
    assert gate.scrub({'messages':[{'content':secret}],'query':secret})['query'] == '[content omitted: credential detected]'


def test_plain_preference_preserved():
    assert gate.scrub('Prefiro respostas concisas em português.') == 'Prefiro respostas concisas em português.'


def test_http_boundary_all_json_and_destination():
    with gate.safe_http_client('http://honcho-api:8000',5) as client:
        hook=client.event_hooks['request'][0]
        req=httpx.Request('POST','http://honcho-api:8000/v3/test',json={'query':'api_key="sk-proj-abcdefghijklmnopqrstuvxyz1234567890"'})
        hook(req)
        assert json.loads(req.read())['query']=='[content omitted: credential detected]'
        assert int(req.headers['content-length'])==len(req.content)
        with pytest.raises(ValueError):
            hook(httpx.Request('POST','https://attacker.test',json={}))
        with pytest.raises(ValueError):
            hook(httpx.Request('POST','http://honcho-api:8000/v3/test',content=b'raw-secret'))
        assert not client.follow_redirects


def test_request_admission_requires_exact_body_session():
    body=b'{"session_id":"s1"}'
    class Request:
        method='POST'
        path='/v1/runs'
        headers={gate.HEADER:proof(body),'X-Hermes-Session-Id':'s1','X-Hermes-Session-Key':'webui:s1'}
        async def read(self): return body
    assert asyncio.run(gate.request_admission(Request(),'default'))['surface']=='webui'
    Request.path='/v1/chat/completions'
    assert asyncio.run(gate.request_admission(Request(),'default')) is None


def test_automation_body_denied():
    body=json.dumps({'session_id':'s1','turn_author':{'is_bot':True}}).encode()
    class Request:
        method='POST'
        path='/v1/runs'
        headers={gate.HEADER:proof(body),'X-Hermes-Session-Id':'s1','X-Hermes-Session-Key':'webui:s1'}
        async def read(self): return body
    assert asyncio.run(gate.request_admission(Request(),'default')) is None


@pytest.mark.parametrize('url,headers',[
    ('http://honcho-api:8000/v3/test?api_key=test-only',{}),
    ('http://honcho-api:8000/v3/test?access_token=test-only',{}),
    ('http://honcho-api:8000/v3/test',{'X-API-Key':'test-only'}),
    ('http://honcho-api:8000/v3/test',{'Cookie':'test-only'}),
    ('http://honcho-api:8000/v3/test',{'Proxy-Authorization':'test-only'}),
    ('http://honcho-api:8000/v3/test',{'Authorization':'Bearer wrong-fixture'}),
    ('http://fixture-user:fixture-pass@honcho-api:8000/v3/test',{}),
])
def test_bodyless_credentials_rejected(url,headers):
    with gate.safe_http_client('http://honcho-api:8000',5) as client:
        hook=client.event_hooks['request'][0]
        with pytest.raises(ValueError): hook(httpx.Request('GET',url,headers=headers))


def test_safe_pagination_and_local_auth_remain_usable():
    with gate.safe_http_client('http://honcho-api:8000',5) as client:
        client.event_hooks['request'][0](httpx.Request('GET',
            'http://honcho-api:8000/v3/test?page=1&size=100&token_budget=1800',
            headers={'Authorization':'Bearer local','Accept':'application/json'}))
