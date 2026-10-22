"""Run: PYTHONPATH=backend backend/.venv/bin/python -m pytest -q tests/test_account_authority.py"""
import base64
import hashlib
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from accounts import Accounts, access, add_duration, term_fields
from web import create_web_app, SESSION_COOKIE

LIFETIME = {'mode': 'lifetime', 'start': 'now'}
DAY = {'mode': 'duration', 'start': 'now', 'amount': 1, 'unit': 'days'}

@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv('ZM_AI_TOOL_OWNER_EMAILS', 'owner@example.com,second@example.com')
    return Accounts(tmp_path)

def owner(store):
    with store.connect() as db:
        return dict(db.execute("SELECT * FROM users WHERE email='owner@example.com'").fetchone())

def create(store, email='user@example.com', **fields):
    return store.mutate(owner(store)['id'], 'accounts', None, {'email': email, **fields})

def denied(code, fn, *args, **kwargs):
    with pytest.raises(HTTPException) as error:
        fn(*args, **kwargs)
    assert error.value.detail == code

def test_migration_seeds_once_and_preserves_identity_workspace(store):
    first = owner(store)
    linked = store.login_identity({'sub': 'google-verified', 'email': first['email'], 'email_verified': True, 'name': 'Google'})
    assert linked == first['id']
    root = store.root / 'users' / linked
    root.mkdir(parents=True)
    (root / 'work.txt').write_text('keep')
    store.mutate(linked, 'accounts', linked, {'role': 'user'})
    again = Accounts(store.root)
    assert again.get(linked)['role'] == 'user'
    assert (root / 'work.txt').read_text() == 'keep'
    assert again.get(linked)['google_sub'] == 'google-verified'
    with again.connect() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 1
    uid = again.login_identity({'sub': 'new-sub', 'email': 'new@example.com', 'email_verified': True})
    assert access(again.get(uid))['state'] == 'pending'
    with pytest.raises(ValueError):
        again.login_identity({'sub': 'fake', 'email': first['email'], 'email_verified': False})

def test_legacy_migration_keeps_sessions_ids_and_makes_backup(tmp_path, monkeypatch):
    monkeypatch.setenv('ZM_AI_TOOL_OWNER_EMAILS', '')
    path = tmp_path / 'accounts.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE users(id TEXT PRIMARY KEY,google_sub TEXT UNIQUE NOT NULL,email TEXT NOT NULL,name TEXT NOT NULL)')
        db.execute("INSERT INTO users VALUES('stable-id','google-sub','old@example.com','Old')")
        db.execute('CREATE TABLE sessions(hash TEXT PRIMARY KEY,user_id TEXT NOT NULL,expires REAL NOT NULL)')
        db.execute("INSERT INTO sessions VALUES('hashed','stable-id',9999999999)")
    migrated = Accounts(tmp_path)
    assert migrated.get('stable-id')['google_sub'] == 'google-sub'
    assert access(migrated.get('stable-id'))['state'] == 'pending'
    assert (tmp_path / 'accounts.before-management.sqlite3').exists()
    with migrated.connect() as db:
        assert db.execute('SELECT user_id FROM sessions').fetchone()[0] == 'stable-id'
    Accounts(tmp_path)

def test_roles_idor_last_owner_and_soft_delete(store):
    oid = owner(store)['id']
    admin = create(store, 'admin@example.com', role='admin', term=LIFETIME)
    reseller = create(store, 'r@example.com', role='reseller', term=LIFETIME, max_accounts=3, max_keys=3, max_days=40)
    other = create(store, 'other@example.com', role='reseller', term=LIFETIME)
    user = create(store, issuer_id=reseller['id'], term=DAY)
    for actor in (user['id'], other['id']):
        denied('ADMIN_FORBIDDEN', store.mutate, actor, 'accounts', user['id'], {'name': 'hijacked'})
    denied('ADMIN_FORBIDDEN', store.mutate, admin['id'], 'accounts', oid, {'name': 'hijacked'})
    denied('ADMIN_FORBIDDEN', store.mutate, admin['id'], 'accounts', user['id'], {'role': 'admin'})
    denied('ADMIN_FORBIDDEN', store.mutate, reseller['id'], 'accounts', user['id'], {'role': 'reseller'})
    denied('ADMIN_FORBIDDEN', store.mutate, reseller['id'], 'accounts', user['id'], {'issuer_id': other['id']})
    assert len(store.list_items(reseller['id'], 'accounts', {})['items']) == 1
    store.mutate(oid, 'accounts', reseller['id'], {'enabled': False})
    denied('ADMIN_FORBIDDEN', store.mutate, reseller['id'], 'accounts', user['id'], {'name': 'fail'})
    assert access(store.get(user['id']))['valid']
    second = next(row for row in store.list_items(oid, 'accounts', {})['items'] if row.get('email') == 'second@example.com')
    store.mutate(oid, 'accounts', second['id'], {}, 'delete')
    for values in ({'enabled': False}, {'role': 'user'}, {'term': {'mode': 'until', 'expiresAt': '2020-01-01T00:00:00Z', 'start': 'scheduled', 'startsAt': '2019-01-01T00:00:00Z'}}):
        denied('LAST_OWNER_REQUIRED', store.mutate, oid, 'accounts', oid, values)
    denied('LAST_OWNER_REQUIRED', store.mutate, oid, 'accounts', oid, {}, 'delete')
    store.mutate(oid, 'accounts', user['id'], {}, 'delete')
    assert store.get(user['id'])['deleted']
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM audit').fetchone()[0] > 0
        changes = json.loads(db.execute("SELECT changes FROM audit WHERE target_id=? ORDER BY id DESC LIMIT 1", (user['id'],)).fetchone()[0])
        assert changes['deleted'] == {'before': 0, 'after': 1}

def test_quota_reservations_and_concurrent_creation(store):
    oid = owner(store)['id']
    reseller = create(store, 'r@example.com', role='reseller', term=LIFETIME)
    denied('RESELLER_QUOTA_EXCEEDED', store.mutate, reseller['id'], 'keys', None, {'term': DAY})
    store.mutate(oid, 'accounts', reseller['id'], {'max_accounts': 1, 'max_keys': 1, 'max_days': 31})
    future = {**DAY, 'start': 'scheduled', 'startsAt': '2030-01-01T00:00:00Z'}
    user = store.mutate(reseller['id'], 'accounts', None, {'email': 'assigned@example.com', 'term': future})
    denied('RESELLER_QUOTA_EXCEEDED', store.mutate, reseller['id'], 'accounts', None, {'email': 'overflow@example.com', 'term': DAY})
    def issue(_):
        try:
            return store.mutate(reseller['id'], 'keys', None, {'term': {**DAY, 'start': 'first_activation'}})
        except HTTPException as exc:
            return exc.detail
    with ThreadPoolExecutor(8) as pool:
        outcomes = list(pool.map(issue, range(8)))
    assert sum(isinstance(result, dict) for result in outcomes) == 1
    assert outcomes.count('RESELLER_QUOTA_EXCEEDED') == 7
    key = next(result for result in outcomes if isinstance(result, dict))
    assert key['access']['state'] == 'unactivated'
    assert 'key_hash' not in key
    store.mutate(oid, 'keys', key['id'], {}, 'delete')
    denied('RESELLER_LIFETIME_FORBIDDEN', store.mutate, reseller['id'], 'keys', None, {'term': LIFETIME})
    denied('RESELLER_TERM_EXCEEDED', store.mutate, reseller['id'], 'keys', None, {'term': {**DAY, 'amount': 32}})
    store.mutate(oid, 'accounts', reseller['id'], {'enabled': False})
    assert access(store.get(user['id']))['state'] == 'scheduled'

def test_devices_first_activation_idempotency_rotation_and_revoke(store):
    oid = owner(store)['id']
    key = store.mutate(oid, 'keys', None, {'term': {**DAY, 'start': 'first_activation'}})
    def activate(index):
        try:
            return store.activate({'key': key['key'], 'installationId': f'device_{index:020d}'})
        except HTTPException as exc:
            return exc.detail
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(activate, range(8)))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count('DEVICE_LIMIT_REACHED') == 7
    index = next(i for i, result in enumerate(results) if isinstance(result, dict))
    first = results[index]
    again = activate(index)
    assert again['deviceToken'] != first['deviceToken']
    assert again['expiresAt'] == first['expiresAt']
    denied('DEVICE_REVOKED', store.device_status, first['deviceToken'])
    assert store.device_status(again['deviceToken'])['valid']
    device = store.device_list(oid, key['id'])[0]
    store.revoke_device(oid, key['id'], device['id'])
    denied('DEVICE_REVOKED', store.device_status, again['deviceToken'])
    new = store.mutate(oid, 'keys', key['id'], {}, 'rotate')
    assert new['key'] != key['key']
    denied('KEY_INVALID', store.activate, {'key': key['key'], 'installationId': 'device_1234567890123456'})
    assert key['key'].encode() not in store.path.read_bytes()

def test_full_keys_survive_restart_and_obey_management_scope(store):
    oid = owner(store)['id']
    reseller = create(store, 'copy-reseller@example.com', role='reseller', term=LIFETIME,
                      max_keys=2, max_days=31)
    other = create(store, 'copy-other@example.com', role='reseller', term=LIFETIME)
    user = create(store, 'copy-user@example.com', term=DAY)
    key = store.mutate(oid, 'keys', None, {'issuer_id': reseller['id'], 'term': DAY})
    reopened = Accounts(store.root)
    listed = reopened.list_items(reseller['id'], 'keys', {})['items'][0]
    assert listed['key'] == key['key']
    assert reopened.list_items(oid, 'keys', {'q': key['key']})['items'][0]['key'] == key['key']
    assert 'key_nonce' not in listed and 'key_hash' not in listed
    assert reopened.list_items(other['id'], 'keys', {})['items'] == []
    denied('ADMIN_FORBIDDEN', reopened.list_items, user['id'], 'keys', {})
    assert reopened.activate({'key': listed['key'], 'installationId': 'copy_test_installation_1234'})['valid']
    rotated = reopened.mutate(oid, 'keys', key['id'], {}, 'rotate')
    assert rotated['key'] != key['key']
    assert reopened.list_items(oid, 'keys', {})['items'][0]['key'] == rotated['key']
    denied('KEY_INVALID', reopened.activate, {'key': key['key'], 'installationId': 'copy_test_installation_1234'})
    with reopened.connect() as db:
        audit = '\n'.join(row['changes'] for row in db.execute('SELECT changes FROM audit'))
    assert key['key'] not in audit and rotated['key'] not in audit and 'key_nonce' not in audit
    assert rotated['key'].encode() not in store.path.read_bytes()
    # Simulate a migrated key with only its original verification hash.
    with reopened.connect(write=True) as db:
        db.execute('UPDATE keys SET key_nonce=NULL WHERE id=?', (key['id'],))
    assert Accounts(store.root).list_items(oid, 'keys', {})['items'][0]['key'] is None
    assert reopened.activate({'key': rotated['key'], 'installationId': 'copy_test_installation_1234'})['valid']

def test_calendar_terms_exact_expiry_and_renewal(store):
    ts = lambda value: datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()
    assert datetime.fromtimestamp(add_duration(ts('2024-01-31'), 1, 'months'), timezone.utc).day == 29
    assert datetime.fromtimestamp(add_duration(ts('2024-02-29'), 1, 'years'), timezone.utc).day == 28
    vietnam_midnight = datetime.fromisoformat('2024-01-31T00:00:00+07:00')
    assert datetime.fromtimestamp(add_duration(vietnam_midnight.timestamp(), 1, 'months'), vietnam_midnight.tzinfo).isoformat() == '2024-02-29T00:00:00+07:00'
    fields = term_fields({'mode': 'duration', 'amount': 1, 'unit': 'hours'}, 'accounts', now=100)
    assert access(fields, now=3700)['state'] == 'expired'
    assert not access({'starts_at': 1}, now=100)['valid']
    assert access(term_fields({'mode': 'lifetime', 'start': 'scheduled', 'startsAt': '2030-01-01T00:00:00Z'}, 'accounts'), now=100)['state'] == 'scheduled'
    account = create(store, term=DAY)
    renewed = store.mutate(owner(store)['id'], 'accounts', account['id'], {'amount': 2, 'unit': 'days'}, 'renew')
    assert abs(datetime.fromisoformat(renewed['access']['expiresAt']).timestamp() - datetime.fromisoformat(account['access']['expiresAt']).timestamp() - 172800) < .01
    with store.connect(write=True) as db:
        db.execute('UPDATE users SET expires_at=1 WHERE id=?', (account['id'],))
    renewed = store.mutate(owner(store)['id'], 'accounts', account['id'], {'amount': 1, 'unit': 'hours'}, 'renew')
    assert 3599 < datetime.fromisoformat(renewed['access']['expiresAt']).timestamp() - time.time() <= 3600

def test_desktop_pkce_single_use_and_loopback(store):
    verifier = 'a' * 43
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    callback = 'http://127.0.0.1:34567/api/auth/desktop/callback'
    for uri in ('https://evil.example/callback', 'http://127.0.0.1:34567/api/auth/desktop/callback?evil=1', 'http://127.0.0.1.evil:34567/api/auth/desktop/callback'):
        denied('INVALID_DESKTOP_REDIRECT', store.desktop_redirect, uri)
    with store.connect(write=True) as db:
        db.execute('INSERT INTO desktop_codes VALUES(?,?,?,?,?)', (store.digest('code'), owner(store)['id'], challenge, callback, time.time() + 60))
    body = {'code': 'code', 'verifier': verifier, 'redirectUri': callback, 'installationId': 'installation_12345678'}
    denied('DESKTOP_CODE_INVALID', store.exchange, {**body, 'verifier': 'wrong' * 10})
    assert store.exchange(body)['mode'] == 'account'
    denied('DESKTOP_CODE_INVALID', store.exchange, body)

def test_http_crud_origin_and_entitlement(tmp_path, monkeypatch):
    monkeypatch.setenv('ZM_AI_TOOL_OWNER_EMAILS', 'owner@example.com')
    monkeypatch.setenv('ZM_AI_TOOL_GOOGLE_CLIENT_ID', '')
    monkeypatch.setenv('ZM_AI_TOOL_GOOGLE_CLIENT_SECRET', '')
    monkeypatch.setenv('ZM_AI_TOOL_WEB_ORIGIN', 'http://localhost:18787')
    app = create_web_app(tmp_path)
    token = app.state.web_auth.create_session({'sub': 'owner-google', 'email': 'owner@example.com', 'email_verified': True})
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE, token)
        user = client.get('/api/auth/session').json()['user']
        assert user['permissions'] == ['accounts', 'keys']
        assert client.post('/api/admin/accounts', json={'email': 'a@example.com'}).status_code == 403
        client.headers.update({'Origin': 'http://localhost:18787', 'X-ZM-Account': user['id']})
        assert client.post('/api/admin/accounts', content='{').status_code == 400
        response = client.post('/api/admin/accounts', json={'email': 'a@example.com'})
        assert response.status_code == 200
        item = response.json()
        assert item['access']['state'] == 'pending'
        assert client.patch('/api/admin/accounts/' + item['id'], json={'term': DAY}).status_code == 200
        assert client.post('/api/admin/accounts/' + item['id'] + '/renew', json={'amount': 1, 'unit': 'days'}).status_code == 200
        assert client.get('/api/admin/accounts?q=a%40').json()['total'] == 1
        key = client.post('/api/admin/keys', json={'term': DAY}).json()
        keys = client.get('/api/admin/keys')
        assert keys.json()['items'][0]['key'] == key['key']
        assert 'no-store' in keys.headers['Cache-Control']
        assert client.delete('/api/admin/accounts/' + item['id']).status_code == 200
        assert client.post('/api/admin/accounts/' + item['id'] + '/restore', json={}).status_code == 200
        assert client.get('/api/license/status').json()['mode'] == 'account'
        assert client.get('/api/admin/audit/history').json()

def test_desktop_real_verification_cache_expiry_and_revocation(tmp_path, monkeypatch):
    from pipeline.core import license as licensing
    monkeypatch.setenv('ZM_AI_TOOL_DESKTOP', '1')
    monkeypatch.delenv('ZM_AI_TOOL_WEB_WORKER', raising=False)
    monkeypatch.setattr(licensing, 'LICENSE_FILE', tmp_path / 'license.json')
    monkeypatch.setattr(licensing, '_cache', None)
    monkeypatch.setattr(licensing, '_checked_at', 0)
    clock = [float(int(time.time()))]
    monkeypatch.setattr(licensing.time, 'time', lambda: clock[0])
    monkeypatch.setattr(licensing.time, 'monotonic', lambda: clock[0])
    def response():
        return {'valid': True, 'configured': True, 'mode': 'key', 'state': 'active', 'lifetime': False,
                'startsAt': datetime.fromtimestamp(clock[0]-100, timezone.utc).isoformat(),
                'expiresAt': datetime.fromtimestamp(clock[0]+3600, timezone.utc).isoformat(), 'deviceToken': 'x'*43}
    with patch.object(licensing, '_request', return_value=response()) as request:
        assert licensing.activate_license('ZMAI-NEW')['valid']
        verified = licensing._read()['verified_at']
        for _ in range(4):
            clock[0] += 60
            assert licensing.license_status()['valid']
        assert request.call_count == 1
        clock[0] += 60
        assert licensing.license_status()['valid']
        assert request.call_count == 2
        verified = licensing._read()['verified_at']
    with patch.object(licensing, '_request', side_effect=OSError('offline')):
        clock[0] = verified + 899
        assert licensing.license_status()['valid']
        # Reading the RAM or disk cache must not extend offline time.
        assert licensing._read()['verified_at'] == verified
        clock[0] = verified + 900
        assert not licensing.license_status()['valid']
        monkeypatch.setattr(licensing, '_cache', None)
        assert not licensing.license_status()['valid']
    with patch.object(licensing, '_request', return_value=response()):
        assert licensing.license_status(force=True)['valid']
    with patch.object(licensing, '_request', return_value={**response(), 'valid': False, 'state': 'blocked'}):
        assert not licensing.license_status(force=True)['valid']
        assert not licensing.license_status()['valid']
    with patch.object(licensing, '_request', return_value=response()):
        assert licensing.license_status(force=True)['valid']
    verified = licensing._read()['verified_at']
    with patch.object(licensing, '_request', return_value={'valid': True}):
        clock[0] += 400
        licensing.license_status(force=True)
        assert licensing._read()['verified_at'] == verified
    cached = response()
    cached['expiresAt'] = datetime.fromtimestamp(clock[0], timezone.utc).isoformat()
    assert not licensing._current(cached, clock[0])['valid']

def test_worker_database_revocation_and_admitted_children(store, monkeypatch):
    from pipeline.core import license as licensing
    monkeypatch.setenv('ZM_AI_TOOL_WEB_WORKER', '1')
    monkeypatch.setenv('ZM_AI_TOOL_WEB_AUTH_DB', str(store.path))
    uid = create(store, term=DAY)['id']
    monkeypatch.setenv('ZM_AI_TOOL_WEB_USER_ID', uid)
    assert licensing.can_start_job()
    with licensing.admitted_job():
        store.mutate(owner(store)['id'], 'accounts', uid, {'enabled': False})
        assert licensing.can_start_job()  # necessary steps finish
    assert not licensing.can_start_job()
    store.mutate(owner(store)['id'], 'accounts', uid, {'enabled': True})
    assert licensing.can_start_job()


def test_unified_queue_pauses_without_running_and_does_not_auto_resume(monkeypatch):
    from pipeline.queue import engine
    from pipeline.core import license as licensing
    obj = engine.QueueEngine.__new__(engine.QueueEngine)
    import threading
    obj._guard = threading.Lock()
    obj._active = {'job': object()}
    changes = []
    with patch.object(licensing, 'can_start_job', return_value=False), patch.object(engine.store, 'mutate', side_effect=lambda uid, data: changes.append(data)), patch.object(obj, '_run_admitted_job') as run:
        obj._run_job('job')
        assert changes[0]['status'] == 'paused'
        run.assert_not_called()
    assert obj._active == {}
    # Only queued rows are scheduled; a paused row needs explicit resume after renewal.
    with patch.object(licensing, 'can_start_job', return_value=True), patch.object(engine.store, 'load_all', return_value=[{'id': 'job', **changes[0]}]), patch.object(obj, '_capacity', return_value=1), patch.object(obj, '_run_admitted_job') as run:
        obj._pause_all = False
        obj._schedule()
        run.assert_not_called()

def test_unactivated_key_renewal_and_future_last_owner_protection(store):
    oid = owner(store)['id']
    key = store.mutate(oid, 'keys', None, {'term': {**DAY, 'start': 'first_activation'}})
    store.mutate(oid, 'keys', key['id'], {'amount': 1, 'unit': 'hours'}, 'renew')
    activated = store.activate({'key': key['key'], 'installationId': 'installation_12345678'})
    assert datetime.fromisoformat(activated['expiresAt']).timestamp() - datetime.fromisoformat(activated['startsAt']).timestamp() == 90000
    with store.connect() as db:
        second = db.execute("SELECT id FROM users WHERE email='second@example.com'").fetchone()[0]
    store.mutate(oid, 'accounts', second, {}, 'delete')
    denied('LAST_OWNER_REQUIRED', store.mutate, oid, 'accounts', oid, {'term': DAY})


def test_running_automation_finishes_pending_pauses_and_requires_resume(tmp_path):
    from pipeline.automation.store import AutomationStore
    from pipeline.automation.service import AutomationService
    from pipeline.core import license as licensing
    import threading
    started, release = threading.Event(), threading.Event()
    calls = []
    def run(job_id):
        calls.append(job_id)
        started.set()
        assert release.wait(5)
        assert licensing.is_admitted()
    db = AutomationStore(tmp_path / 'automation.sqlite3', tmp_path / 'jobs')
    service = AutomationService(store=db, runner=run, max_workers=1)
    first = db.create_job('topic', 'First', {})
    second = db.create_job('topic', 'Waiting', {})
    try:
        with patch.object(licensing, 'can_start_job', return_value=True):
            service.start_job(first['id'])
            assert started.wait(3)
            service.start_job(second['id'])
            licensing.pause_pending_jobs()
            assert db.get_job(second['id'])['status'] == 'paused'
            release.set()
            service._futures[first['id']].result(timeout=3)
            assert db.get_job(first['id'])['status'] == 'completed'
            assert calls == [first['id']]
            service.resume_job(second['id'])
            service._futures[second['id']].result(timeout=3)
            assert db.get_job(second['id'])['status'] == 'completed'
    finally:
        release.set()
        service.close()


def test_waiting_flow_job_blocks_before_capacity_is_available():
    from pipeline.flow.worker import FlowWorkerMixin
    from pipeline.flow import store as flow_store
    from pipeline.core import license as licensing
    from types import SimpleNamespace
    import threading
    row = {'id': 'waiting-flow', 'accountId': 'provider-account', 'status': 'queued', 'kind': 'video', 'queueOrder': 0, 'settings': {'concurrency': 1}}
    fake = SimpleNamespace(_account_condition=threading.Condition(), _running_jobs=set(), _cancelled=set(), _account_next_start={}, _account_active={'provider-account': 1})
    changes = []
    with patch.object(flow_store, 'get_row', side_effect=lambda kind, uid: row if kind == 'jobs' else {}), patch.object(flow_store, 'list_rows', return_value=[row]), patch.object(flow_store, 'patch_row', side_effect=lambda kind, uid, fields: changes.append(fields)), patch.object(licensing, 'can_start_job', return_value=False):
        FlowWorkerMixin._run_sync(fake, 'waiting-flow')
    assert changes[0]['status'] == 'action_required'
    assert changes[0]['error'] == 'ACCESS_REQUIRED'
    assert not fake._running_jobs

def test_concurrent_http_quota_and_device_limits(tmp_path, monkeypatch):
    monkeypatch.setenv('ZM_AI_TOOL_OWNER_EMAILS', 'owner@example.com')
    monkeypatch.setenv('ZM_AI_TOOL_GOOGLE_CLIENT_ID', '')
    monkeypatch.setenv('ZM_AI_TOOL_GOOGLE_CLIENT_SECRET', '')
    monkeypatch.setenv('ZM_AI_TOOL_WEB_ORIGIN', 'http://localhost:18787')
    app = create_web_app(tmp_path)
    auth = app.state.web_auth
    with auth.connect() as db:
        oid = db.execute("SELECT id FROM users WHERE role='owner'").fetchone()[0]
    reseller = auth.mutate(oid, 'accounts', None, {'email': 'reseller@example.com', 'role': 'reseller', 'term': LIFETIME, 'max_accounts': 1, 'max_keys': 1, 'max_days': 31})
    token = auth.create_session({'sub': 'reseller-google', 'email': 'reseller@example.com', 'email_verified': True})
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE, token)
        client.headers.update({'Origin': 'http://localhost:18787', 'X-ZM-Account': reseller['id']})
        def account_request(index):
            return client.post('/api/admin/accounts', json={'email': f'customer{index}@example.com', 'term': DAY}).status_code
        with ThreadPoolExecutor(8) as pool:
            codes = list(pool.map(account_request, range(8)))
        assert codes.count(200) == 1 and codes.count(409) == 7
        key = client.post('/api/admin/keys', json={'term': {**DAY, 'start': 'first_activation'}}).json()
        def device_request(index):
            return client.post('/api/desktop/activate', json={'key': key['key'], 'installationId': f'installation_{index:016d}'}).status_code
        with ThreadPoolExecutor(8) as pool:
            codes = list(pool.map(device_request, range(8)))
        assert codes.count(200) == 1 and codes.count(409) == 7


def test_local_app_rejects_cross_site_signout():
    from api.app import create_app
    client = TestClient(create_app())
    try:
        response = client.post('/api/license/deactivate', headers={'Origin': 'https://untrusted.example'})
        assert response.status_code == 403
        assert response.json()['detail'] == 'WEB_ORIGIN_REJECTED'
    finally:
        client.close()


@pytest.fixture
def desktop(store, tmp_path, monkeypatch):
    from pipeline.core import license as licensing
    monkeypatch.setenv('ZM_AI_TOOL_DESKTOP', '1')
    monkeypatch.setenv('ZM_AI_TOOL_AUTH_SERVER_URL', 'https://authority.example')
    monkeypatch.delenv('ZM_AI_TOOL_WEB_WORKER', raising=False)
    monkeypatch.setattr(licensing, 'LICENSE_FILE', tmp_path / 'desktop-license.json')
    monkeypatch.setattr(licensing, 'ensure_data_dirs', lambda: None)
    monkeypatch.setattr(licensing, '_cache', None)
    monkeypatch.setattr(licensing, '_checked_at', 0)
    monkeypatch.setattr(licensing, '_pending', {})
    monkeypatch.setattr(licensing.webbrowser, 'open', lambda _: True)
    def request(action, body):
        try:
            if action == 'activate':
                return store.activate(body)
            if action == 'exchange':
                return store.exchange(body)
            return store.device_status(body['deviceToken'], logout=action == 'logout')
        except HTTPException as exc:
            raise ValueError(exc.detail) from None
    monkeypatch.setattr(licensing, '_request', request)
    return licensing


def test_app_key_status_matches_authority_configuration_and_releases_slot(store, desktop):
    from api.app import create_app
    key = store.mutate(owner(store)['id'], 'keys', None, {'term': DAY, 'max_devices': 2})
    with TestClient(create_app()) as client:
        status = client.post('/api/license/activate', json={'key': key['key']}).json()
        assert status['valid'] and status['mode'] == 'key' and status['configured']
        assert status['keyMasked'] == key['key_masked']
        assert status['expiresAt'] == key['access']['expiresAt']
        assert (status['activationCount'], status['activationLimit']) == (1, 2)
        assert 'deviceToken' not in status and key['key'] not in json.dumps(status)
        assert desktop._read()['authority'] == 'https://authority.example'
        store.mutate(owner(store)['id'], 'keys', key['id'], {'max_devices': 3, 'term': LIFETIME})
        refreshed = client.get('/api/license/status?force=true').json()
        assert refreshed['activationLimit'] == 3 and refreshed['remainingDay'] == -1
        assert client.post('/api/auth/desktop/start').json()['detail'] == 'DEVICE_ALREADY_CONFIGURED'
        assert client.post('/api/license/activate', json={'key': key['key']}).json()['detail'] == 'DEVICE_ALREADY_CONFIGURED'
        assert not client.post('/api/license/deactivate').json()['configured']
        assert not store.device_list(owner(store)['id'], key['id'])[0]['enabled']
        assert client.post('/api/license/activate', json={'key': key['key']}).json()['activationCount'] == 1


def test_app_account_and_key_are_exclusive_and_late_callback_cannot_replace_key(store, desktop):
    login = parse_qs(urlsplit(desktop.start_desktop_login(34567)['authorizationUrl']).query)
    uid = create(store, 'desktop@example.com', term=DAY)['id']
    with store.connect(write=True) as db:
        db.execute('INSERT INTO desktop_codes VALUES(?,?,?,?,?)',
                   (store.digest('account-code'), uid, login['challenge'][0], login['redirect_uri'][0], time.time() + 60))
    desktop.finish_desktop_login(login['state'][0], 'account-code')
    status = desktop.license_status()
    assert status['mode'] == 'account' and status['user']['email'] == 'desktop@example.com'
    with pytest.raises(ValueError, match='DEVICE_ALREADY_CONFIGURED'):
        desktop.activate_license('ZMAI-TEST')
    account_token = desktop._read()['device_token']
    desktop.deactivate_license()
    denied('DEVICE_REVOKED', store.device_status, account_token)
    pending = parse_qs(urlsplit(desktop.start_desktop_login(34567)['authorizationUrl']).query)
    key = store.mutate(owner(store)['id'], 'keys', None, {'term': DAY})
    desktop.activate_license(key['key'])
    with pytest.raises(ValueError, match='DESKTOP_CODE_INVALID'):
        desktop.finish_desktop_login(pending['state'][0], 'late-code')
    assert desktop.license_status()['mode'] == 'key'


def test_app_deactivation_preserves_offline_credentials_but_clears_revoked_device(store, desktop):
    key = store.mutate(owner(store)['id'], 'keys', None, {'term': DAY})
    desktop.activate_license(key['key'])
    token = desktop._read()['device_token']
    with patch.object(desktop, '_request', side_effect=OSError('offline')):
        with pytest.raises(ValueError, match='LOGOUT_NETWORK_REQUIRED'):
            desktop.deactivate_license()
    assert desktop._read()['device_token'] == token
    store.device_status(token, logout=True)
    assert not desktop.deactivate_license()['configured']
    assert 'device_token' not in desktop._read()


def test_app_rejects_wrong_activation_method_without_installing_credentials(store, desktop):
    with patch.object(desktop, '_request', return_value={'deviceToken': 'x' * 43, 'valid': True, 'mode': 'account'}):
        with pytest.raises(RuntimeError, match='INVALID_LICENSE_RESPONSE'):
            desktop.activate_license('ZMAI-TEST')
    assert 'device_token' not in desktop._read()


def test_app_cached_remaining_days_follow_expiry(desktop):
    now = time.time()
    status = {'valid': True, 'remainingDay': 9, 'expiresAt': datetime.fromtimestamp(now + 100, timezone.utc).isoformat()}
    assert desktop._current(status, now)['remainingDay'] == 1
