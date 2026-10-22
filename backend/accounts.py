"""Account/key authority for the single-host Web server (SQLite, no external key API)."""
from __future__ import annotations

import base64
import calendar
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import time
import threading
from collections import defaultdict, deque
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
import anyio

ROLES = ('owner', 'admin', 'reseller', 'user')
RANK = {role: i for i, role in enumerate(ROLES)}
UTC = timezone.utc


def fail(code, status=422):
    raise HTTPException(status, code)


def iso(value):
    return datetime.fromtimestamp(value, UTC).isoformat() if value is not None else None


def timestamp(value):
    try:
        date = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if date.tzinfo is None:
            fail('TIMEZONE_REQUIRED')
        return date.timestamp()
    except (ValueError, TypeError, OverflowError):
        fail('INVALID_TERM')


def integer(value, minimum=0, maximum=1_000_000):
    if type(value) is not int or not minimum <= value <= maximum:
        fail('INVALID_NUMBER')
    return value


def add_duration(start, amount, unit):
    integer(amount, 1, 10000)
    date = datetime.fromtimestamp(start, timezone(timedelta(hours=7)))
    try:
        if unit in ('hours', 'days'):
            return (date + timedelta(**{unit: amount})).timestamp()
        if unit not in ('months', 'years'):
            fail('INVALID_TERM')
        month = date.year * 12 + date.month - 1 + amount * (12 if unit == 'years' else 1)
        year, month = divmod(month, 12)
        month += 1
        return date.replace(year=year, month=month, day=min(date.day, calendar.monthrange(year, month)[1])).timestamp()
    except (ValueError, OverflowError):
        fail('INVALID_TERM')


def term_fields(term, kind, now=None):
    now = time.time() if now is None else now
    if not isinstance(term, dict) or set(term) - {'mode', 'start', 'startsAt', 'expiresAt', 'amount', 'unit'}:
        fail('INVALID_TERM')
    mode, start = term.get('mode'), term.get('start', 'now')
    if mode not in ('duration', 'until', 'lifetime') or start not in ('now', 'scheduled', 'first_activation'):
        fail('INVALID_TERM')
    if start == 'first_activation' and (kind != 'keys' or mode == 'until'):
        fail('INVALID_TERM')
    begins = None if start == 'first_activation' else timestamp(term.get('startsAt')) if start == 'scheduled' else now
    ends = None
    if mode == 'duration':
        ends = add_duration(begins if begins is not None else now, term.get('amount'), term.get('unit'))
        if begins is None:
            ends = None
    elif mode == 'until':
        ends = timestamp(term.get('expiresAt'))
        if ends <= begins:
            fail('INVALID_TERM')
    return {'starts_at': begins, 'expires_at': ends, 'lifetime': int(mode == 'lifetime'), 'term': json.dumps(term)}


def access(row, now=None):
    now = time.time() if now is None else now
    if row.get('deleted'):
        state = 'deleted'
    elif not row.get('enabled', 1):
        state = 'blocked'
    elif row.get('starts_at') is None:
        state = 'unactivated' if row.get('term') else 'pending'
    elif row['starts_at'] > now:
        state = 'scheduled'
    elif not row.get('lifetime') and (row.get('expires_at') is None or row['expires_at'] <= now):
        state = 'expired'
    else:
        state = 'active'
    return {'state': state, 'valid': state == 'active', 'startsAt': iso(row.get('starts_at')),
            'expiresAt': iso(row.get('expires_at')), 'lifetime': bool(row.get('lifetime'))}


def license_status(row, mode='account'):
    grant = access(row)
    return {**grant, 'mode': mode, 'configured': True, 'keyMasked': row.get('key_masked', ''),
            'remainingDay': -1 if grant['lifetime'] else max(0, math.ceil(((row.get('expires_at') or 0) - time.time()) / 86400)),
            'activationLimit': row.get('max_devices', 0), 'message': '', 'code': 'ACCESS_' + grant['state'].upper()}


class Accounts:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = root / 'accounts.sqlite3'
        if self.path.exists():
            with sqlite3.connect(self.path) as old:
                version = old.execute('PRAGMA user_version').fetchone()[0]
                if version > 1:
                    raise RuntimeError('Account database schema is newer than this server')
                backup = root / 'accounts.before-management.sqlite3'
                if version == 0 and not backup.exists():
                    with sqlite3.connect(backup) as target:
                        old.backup(target)
                    backup.chmod(0o600)
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, google_sub TEXT UNIQUE NOT NULL,
                    email TEXT NOT NULL, name TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS logins (hash TEXT PRIMARY KEY, state TEXT NOT NULL, nonce TEXT NOT NULL,
                    verifier TEXT NOT NULL, return_to TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS keys (id TEXT PRIMARY KEY, key_hash TEXT UNIQUE NOT NULL,
                    key_masked TEXT NOT NULL, name TEXT NOT NULL DEFAULT '', issuer_id TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1, deleted INTEGER NOT NULL DEFAULT 0,
                    notes TEXT NOT NULL DEFAULT '', starts_at REAL, expires_at REAL, lifetime INTEGER NOT NULL DEFAULT 0,
                    term TEXT, max_devices INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS devices (id TEXT PRIMARY KEY, installation_id TEXT NOT NULL,
                    key_id TEXT, user_id TEXT, token_hash TEXT UNIQUE NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                    name TEXT NOT NULL, created_at REAL NOT NULL, last_seen REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS devices_key ON devices(key_id);
                CREATE TABLE IF NOT EXISTS desktop_codes (hash TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                    challenge TEXT NOT NULL, redirect_uri TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, actor_id TEXT NOT NULL, action TEXT NOT NULL,
                    kind TEXT NOT NULL, target_id TEXT NOT NULL, changes TEXT NOT NULL, created_at REAL NOT NULL);
            ''')
            columns = {item['name'] for item in db.execute('PRAGMA table_info(users)')}
            for name, definition in {
                'role': "TEXT NOT NULL DEFAULT 'user'", 'enabled': 'INTEGER NOT NULL DEFAULT 1',
                'deleted': 'INTEGER NOT NULL DEFAULT 0', 'issuer_id': 'TEXT', 'notes': "TEXT NOT NULL DEFAULT ''",
                'starts_at': 'REAL', 'expires_at': 'REAL', 'lifetime': 'INTEGER NOT NULL DEFAULT 0', 'term': 'TEXT',
                'max_accounts': 'INTEGER NOT NULL DEFAULT 0', 'max_keys': 'INTEGER NOT NULL DEFAULT 0',
                'max_days': 'INTEGER NOT NULL DEFAULT 0', 'allow_lifetime': 'INTEGER NOT NULL DEFAULT 0',
                'created_at': 'REAL NOT NULL DEFAULT 0',
            }.items():
                if name not in columns:
                    db.execute(f'ALTER TABLE users ADD COLUMN {name} {definition}')
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS users_email ON users(lower(email))')
            key_columns = {item['name'] for item in db.execute('PRAGMA table_info(keys)')}
            for name in ('created_by', 'key_nonce'):
                if name not in key_columns:
                    db.execute(f'ALTER TABLE keys ADD COLUMN {name} TEXT')
            db.execute('BEGIN IMMEDIATE')
            db.execute("INSERT OR IGNORE INTO settings(name,value) VALUES('key_secret',?)", (secrets.token_hex(32),))
            owners = [email.strip().lower() for email in os.environ.get('ZM_AI_TOOL_OWNER_EMAILS', '').split(',') if email.strip()]
            if owners and not db.execute("SELECT 1 FROM settings WHERE name='owners_seeded'").fetchone():
                for email in owners:
                    self.email(email)
                    row = db.execute('SELECT id FROM users WHERE lower(email)=?', (email,)).fetchone()
                    if not row:
                        uid = secrets.token_hex(16)
                        db.execute('INSERT INTO users(id,google_sub,email,name,created_at) VALUES(?,?,?,?,?)',
                                   (uid, 'pending:' + uid, email, email.split('@')[0], time.time()))
                    else:
                        uid = row['id']
                    db.execute("UPDATE users SET role='owner',enabled=1,deleted=0,starts_at=?,lifetime=1,expires_at=NULL WHERE id=?", (time.time(), uid))
                db.execute("INSERT INTO settings VALUES('owners_seeded','1')")
            db.execute('PRAGMA user_version=1')
        self.path.chmod(0o600)

    @contextmanager
    def connect(self, write=False):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                if write:
                    db.execute('BEGIN IMMEDIATE')
                yield db
        finally:
            db.close()

    @staticmethod
    def digest(value):
        return hashlib.sha256(value.encode()).hexdigest()

    @staticmethod
    def key_value(db, row):
        if not row.get('key_nonce'):
            return None
        # ponytail: HMAC makes new keys recoverable; legacy hash-only keys require explicit rotation.
        secret = db.execute("SELECT value FROM settings WHERE name='key_secret'").fetchone()['value']
        value = hmac.new(secret.encode(), (row['id'] + ':' + row['key_nonce']).encode(), hashlib.sha256).hexdigest()[:32]
        return 'ZMAI-' + value.upper()

    @staticmethod
    def email(value):
        if not isinstance(value, str) or len(value) > 254 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', value):
            fail('INVALID_EMAIL')
        return value.strip().lower()

    def get(self, uid, db=None):
        if db is None:
            with self.connect() as conn:
                return self.get(uid, conn)
        row = db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
        return dict(row) if row else None

    def login_identity(self, identity):
        sub = identity.get('sub')
        if not isinstance(sub, str) or not sub or identity.get('email_verified') is not True:
            raise ValueError('Unverified Google identity')
        email = self.email(identity.get('email'))
        with self.connect(write=True) as db:
            row = db.execute('SELECT * FROM users WHERE google_sub=?', (sub,)).fetchone()
            if row is None:
                row = db.execute('SELECT * FROM users WHERE lower(email)=?', (email,)).fetchone()
                if row and not row['google_sub'].startswith('pending:'):
                    fail('GOOGLE_IDENTITY_CONFLICT', 403)
            uid = row['id'] if row else self.digest('google:' + sub)
            if row and row['deleted']:
                fail('ACCOUNT_DELETED', 403)
            if row:
                db.execute('UPDATE users SET google_sub=?,email=?,name=? WHERE id=?',
                           (sub, email, row['name'] or str(identity.get('name') or '')[:200], uid))
            else:
                db.execute('INSERT INTO users(id,google_sub,email,name,created_at) VALUES(?,?,?,?,?)',
                           (uid, sub, email, str(identity.get('name') or '')[:200], time.time()))
            return uid

    def public(self, row, kind='accounts'):
        result = {k: v for k, v in row.items() if k not in {'google_sub', 'key_hash', 'token_hash', 'key_nonce'}}
        result['access'] = access(row)
        result['term'] = json.loads(row['term']) if row.get('term') else None
        if kind == 'accounts':
            result['linked'] = not row['google_sub'].startswith('pending:')
            result['permissions'] = ['accounts', 'keys'] if self.can_manage(row) else []
        return result

    @staticmethod
    def can_manage(actor):
        return (not actor.get('deleted') and bool(actor.get('enabled')) and
                (actor['role'] in ('owner', 'admin') or actor['role'] == 'reseller' and access(actor)['valid']))

    def authorize(self, actor, target=None):
        if not actor or not self.can_manage(actor):
            fail('ADMIN_FORBIDDEN', 403)
        if target:
            if actor['role'] == 'reseller' and (target.get('issuer_id') != actor['id'] or target.get('role', 'user') != 'user'):
                fail('ADMIN_FORBIDDEN', 403)
            if actor['role'] == 'admin' and target.get('role') in ('owner', 'admin'):
                fail('ADMIN_FORBIDDEN', 403)

    def audit(self, db, actor, kind, uid, action, data):
        safe = {k: v for k, v in data.items() if k not in {'key_hash', 'google_sub', 'token_hash', 'key_nonce'}}
        db.execute('INSERT INTO audit(actor_id,action,kind,target_id,changes,created_at) VALUES(?,?,?,?,?,?)',
                   (actor['id'], action, kind, uid, json.dumps(safe), time.time()))

    def check_quota(self, db, row, kind):
        issuer = self.get(row.get('issuer_id'), db)
        if not issuer or issuer['role'] != 'reseller' or access(row)['state'] in ('deleted', 'blocked', 'expired', 'pending'):
            return
        table, limit = ('users', 'max_accounts') if kind == 'accounts' else ('keys', 'max_keys')
        others = [dict(item) for item in db.execute(f'SELECT * FROM {table} WHERE issuer_id=? AND id!=?', (issuer['id'], row['id']))]
        used = sum(access(item)['state'] not in ('deleted', 'blocked', 'expired', 'pending') for item in others)
        if used >= issuer[limit]:
            fail('RESELLER_QUOTA_EXCEEDED', 409)
        if row.get('lifetime'):
            if not issuer['allow_lifetime']:
                fail('RESELLER_LIFETIME_FORBIDDEN', 403)
        else:
            term = json.loads(row.get('term') or '{}')
            # Reserve the maximum calendar duration for a key activated in a future month/year.
            if row.get('starts_at') is None:
                units = {'hours': 1 / 24, 'days': 1, 'months': 31, 'years': 366}
                days = sum(part.get('amount', 0) * units.get(part.get('unit'), 0) for part in [term, *term.get('extensions', [])])
            else:
                days = ((row.get('expires_at') or row['starts_at']) - max(time.time(), row['starts_at'])) / 86400
            if days <= 0 or days > issuer['max_days']:
                fail('RESELLER_TERM_EXCEEDED', 403)

    def list_items(self, actor_id, kind, query):
        with self.connect() as db:
            actor = self.get(actor_id, db)
            self.authorize(actor)
            table = 'users' if kind == 'accounts' else 'keys'
            rows = [dict(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY created_at DESC,id')]
            if actor['role'] == 'reseller':
                rows = [r for r in rows if r['issuer_id'] == actor['id'] and r.get('role', 'user') == 'user']
            search = str(query.get('q', '')).lower().strip()
            rows = [r for r in rows if (not r['deleted'] or query.get('state') == 'deleted')
                    and (not search or search in ' '.join(str(r.get(k, '')) for k in ('name', 'email', 'key_masked', 'notes')).lower()
                         or kind == 'keys' and self.digest(search.upper()) == r['key_hash'])
                    and (not query.get('role') or r.get('role') == query['role'])
                    and (not query.get('issuer_id') or r.get('issuer_id') == query['issuer_id'])
                    and (not query.get('created_by') or r.get('created_by') == query['created_by'])
                    and (not query.get('term_mode') or json.loads(r.get('term') or '{}').get('mode') == query['term_mode'])
                    and (not query.get('state') or access(r)['state'] == query['state'])]
            try:
                page = max(1, min(100000, int(query.get('page', 1))))
            except (ValueError, TypeError):
                fail('INVALID_NUMBER')
            items = []
            for row in rows[(page - 1) * 25:page * 25]:
                item = self.public(row, kind)
                if kind == 'keys':
                    value = self.key_value(db, row)
                    item['key'] = value if value and secrets.compare_digest(self.digest(value), row['key_hash']) else None
                items.append(item)
            return {'items': items, 'total': len(rows), 'page': page, 'pageSize': 25}

    def mutate(self, actor_id, kind, uid, data, action='update'):
        if kind not in ('accounts', 'keys') or action == 'rotate' and kind != 'keys' or action == 'revoke-sessions' and kind != 'accounts':
            fail('INVALID_ACTION')
        table = 'users' if kind == 'accounts' else 'keys'
        allowed = {'name', 'notes', 'enabled', 'issuer_id', 'term'} | ({'email', 'role', 'max_accounts', 'max_keys', 'max_days', 'allow_lifetime'} if kind == 'accounts' else {'max_devices'})
        if not isinstance(data, dict) or (action == 'update' and set(data) - allowed):
            fail('INVALID_FIELDS')
        with self.connect(write=True) as db:
            actor = self.get(actor_id, db)
            self.authorize(actor)
            old = db.execute(f'SELECT * FROM {table} WHERE id=?', (uid,)).fetchone() if uid else None
            if uid and not old:
                fail('NOT_FOUND', 404)
            if old:
                old = dict(old)
                self.authorize(actor, old)
                row = dict(old)
            else:
                uid = secrets.token_hex(16)
                row = {'id': uid, 'name': '', 'notes': '', 'enabled': 1, 'deleted': 0, 'issuer_id': actor['id'] if actor['role'] == 'reseller' else None,
                       'starts_at': None, 'expires_at': None, 'lifetime': 0, 'term': None, 'created_at': time.time()}
                if kind == 'accounts':
                    row.update(email='', google_sub='pending:' + uid, role='user', max_accounts=0, max_keys=0, max_days=0, allow_lifetime=0)
                else:
                    row.update(max_devices=1, created_by=actor['id'])
            raw_key = None
            if old and old['deleted'] and action != 'restore':
                fail('ACCOUNT_DELETED', 409)
            if action == 'update':
                for field, value in data.items():
                    if field == 'term':
                        row.update(term_fields(value, kind))
                    elif field in ('enabled', 'allow_lifetime'):
                        if type(value) is not bool:
                            fail('INVALID_FIELDS')
                        row[field] = int(value)
                    elif field.startswith('max_'):
                        row[field] = integer(value, 1 if field == 'max_devices' else 0)
                    elif field in ('name', 'notes'):
                        if not isinstance(value, str) or len(value) > (200 if field == 'name' else 4000):
                            fail('INVALID_FIELDS')
                        row[field] = value.strip()
                    elif field == 'email':
                        email = self.email(value)
                        if old and not old['google_sub'].startswith('pending:') and email != old['email']:
                            fail('GOOGLE_EMAIL_LINKED', 409)
                        row[field] = email
                    elif field == 'role':
                        if value not in ROLES or RANK[value] <= RANK[actor['role']] and actor['role'] != 'owner':
                            fail('ADMIN_FORBIDDEN', 403)
                        row[field] = value
                    elif field == 'issuer_id':
                        issuer = self.get(value, db) if value else None
                        if value and (not issuer or issuer['role'] != 'reseller' or issuer['deleted']):
                            fail('INVALID_ISSUER')
                        row[field] = value or None
                if actor['role'] == 'reseller' and (set(data) & {'max_accounts', 'max_keys', 'max_days', 'allow_lifetime'} or row['issuer_id'] != actor['id']):
                    fail('ADMIN_FORBIDDEN', 403)
                if kind == 'accounts':
                    self.email(row['email'])
                elif not row['term']:
                    fail('INVALID_TERM')
            elif action == 'renew':
                if set(data) != {'amount', 'unit'}:
                    fail('INVALID_TERM')
                if row['lifetime'] or not row['term']:
                    fail('TERM_REPLACE_REQUIRED', 409)
                if row['starts_at'] is None:
                    term = json.loads(row['term'])
                    if kind != 'keys' or term['mode'] != 'duration':
                        fail('TERM_REPLACE_REQUIRED', 409)
                    add_duration(time.time(), data['amount'], data['unit'])
                    term.setdefault('extensions', []).append(data)
                    row['term'] = json.dumps(term)
                else:
                    row['expires_at'] = add_duration(max(time.time(), row['expires_at'] or 0), data['amount'], data['unit'])
                    row['term'] = json.dumps({'mode': 'until', 'start': 'scheduled', 'startsAt': iso(row['starts_at']), 'expiresAt': iso(row['expires_at'])})
            elif action == 'delete':
                row.update(deleted=1, enabled=0)
            elif action == 'restore':
                row.update(deleted=0, enabled=1)
            elif action not in ('revoke-sessions', 'rotate'):
                fail('INVALID_ACTION')
            if kind == 'accounts' and old and old['role'] == 'owner':
                owners = [dict(r) for r in db.execute("SELECT * FROM users WHERE role='owner' AND id!=?", (uid,))]
                if row['role'] == 'owner':
                    owners.append(row)
                # Keep continuous owner coverage, including scheduled handovers and future expiry.
                covered = time.time()
                for candidate in sorted(owners, key=lambda item: item['starts_at'] if item['starts_at'] is not None else float('inf')):
                    if candidate['deleted'] or not candidate['enabled'] or candidate['starts_at'] is None or candidate['starts_at'] > covered:
                        continue
                    covered = max(covered, float('inf') if candidate['lifetime'] else candidate['expires_at'] or 0)
                if covered != float('inf'):
                    fail('LAST_OWNER_REQUIRED', 409)
            self.check_quota(db, row, kind)
            if kind == 'keys' and old and row['max_devices'] < db.execute('SELECT count(*) FROM devices WHERE key_id=? AND enabled=1', (uid,)).fetchone()[0] and action != 'rotate':
                fail('DEVICE_LIMIT_BELOW_ACTIVE', 409)
            if kind == 'keys' and (not old or action == 'rotate'):
                row['key_nonce'] = secrets.token_hex(16)
                raw_key = self.key_value(db, row)
                row.update(key_hash=self.digest(raw_key), key_masked=raw_key[:9] + '••••' + raw_key[-4:])
                if action == 'rotate':
                    db.execute('UPDATE devices SET enabled=0 WHERE key_id=?', (uid,))
            if old:
                fields = [k for k in row if k != 'id']
                db.execute(f"UPDATE {table} SET " + ','.join(f'{k}=?' for k in fields) + ' WHERE id=?', [row[k] for k in fields] + [uid])
            else:
                db.execute(f"INSERT INTO {table}(" + ','.join(row) + ') VALUES(' + ','.join('?' for _ in row) + ')', list(row.values()))
            if kind == 'accounts' and (action in ('delete', 'revoke-sessions') or old and old['role'] != row['role']):
                db.execute('DELETE FROM sessions WHERE user_id=?', (uid,))
                db.execute('DELETE FROM desktop_codes WHERE user_id=?', (uid,))
                db.execute('UPDATE devices SET enabled=0 WHERE user_id=?', (uid,))
            self.audit(db, actor, kind, uid, action, {k: {'before': old.get(k) if old else None, 'after': v} for k, v in row.items() if (not old or old.get(k) != v) and k not in {'key_hash', 'google_sub', 'key_nonce'}})
            return {**self.public(row, kind), **({'key': raw_key} if raw_key else {})}

    def device_list(self, actor_id, key_id):
        with self.connect() as db:
            actor = self.get(actor_id, db)
            row = db.execute('SELECT * FROM keys WHERE id=?', (key_id,)).fetchone()
            if not row:
                fail('NOT_FOUND', 404)
            self.authorize(actor, dict(row))
            return [dict(r) for r in db.execute('SELECT id,name,installation_id,enabled,created_at,last_seen FROM devices WHERE key_id=? ORDER BY created_at DESC', (key_id,))]

    def revoke_device(self, actor_id, key_id, device_id):
        with self.connect(write=True) as db:
            actor = self.get(actor_id, db)
            row = db.execute('SELECT * FROM keys WHERE id=?', (key_id,)).fetchone()
            if not row:
                fail('NOT_FOUND', 404)
            self.authorize(actor, dict(row))
            db.execute('UPDATE devices SET enabled=0 WHERE id=? AND key_id=?', (device_id, key_id))
            self.audit(db, actor, 'keys', key_id, 'revoke-device', {'device_id': device_id})
        return {'ok': True}

    def issue_device(self, db, row, installation, name, kind):
        if not isinstance(installation, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{16,128}', installation) or not isinstance(name, str) or len(name) > 200:
            fail('INVALID_DEVICE')
        field = 'key_id' if kind == 'key' else 'user_id'
        previous = db.execute(f'SELECT * FROM devices WHERE {field}=? AND installation_id=?', (row['id'], installation)).fetchone()
        if kind == 'key':
            count = db.execute('SELECT count(*) FROM devices WHERE key_id=? AND enabled=1', (row['id'],)).fetchone()[0]
            if not (previous and previous['enabled']) and count >= row['max_devices']:
                fail('DEVICE_LIMIT_REACHED', 409)
        token, now = secrets.token_urlsafe(32), time.time()
        if previous:
            db.execute('UPDATE devices SET token_hash=?,enabled=1,name=?,last_seen=? WHERE id=?', (self.digest(token), name, now, previous['id']))
        else:
            db.execute(f'INSERT INTO devices(id,installation_id,{field},token_hash,name,created_at,last_seen) VALUES(?,?,?,?,?,?,?)',
                       (secrets.token_hex(16), installation, row['id'], self.digest(token), name, now, now))
        return {'deviceToken': token, **license_status(row, kind), 'user': self.public(row) if kind == 'account' else None}

    def activate(self, body):
        if set(body) - {'key', 'installationId', 'name'} or not isinstance(body.get('key'), str):
            fail('INVALID_FIELDS')
        with self.connect(write=True) as db:
            found = db.execute('SELECT * FROM keys WHERE key_hash=?', (self.digest(body['key'].strip()),)).fetchone()
            if not found:
                fail('KEY_INVALID', 403)
            row = dict(found)
            if access(row)['state'] == 'unactivated':
                term = json.loads(row['term'])
                term['start'] = 'now'
                extensions = term.pop('extensions', [])
                row.update(term_fields(term, 'keys'))
                for extension in extensions:
                    row['expires_at'] = add_duration(row['expires_at'], extension['amount'], extension['unit'])
                db.execute('UPDATE keys SET starts_at=?,expires_at=? WHERE id=?', (row['starts_at'], row['expires_at'], row['id']))
            if not access(row)['valid']:
                fail('ACCESS_' + access(row)['state'].upper(), 403)
            return self.issue_device(db, row, body.get('installationId'), body.get('name', ''), 'key')

    def device_status(self, token, logout=False):
        if not isinstance(token, str) or not 20 <= len(token) <= 200:
            fail('DEVICE_REVOKED', 401)
        with self.connect(write=True) as db:
            device = db.execute('SELECT * FROM devices WHERE token_hash=?', (self.digest(token),)).fetchone()
            if not device or not device['enabled']:
                fail('DEVICE_REVOKED', 401)
            if logout:
                db.execute('UPDATE devices SET enabled=0 WHERE id=?', (device['id'],))
                return {'ok': True}
            field, mode = ('keys', 'key') if device['key_id'] else ('users', 'account')
            row = dict(db.execute(f'SELECT * FROM {field} WHERE id=?', (device['key_id'] or device['user_id'],)).fetchone())
            db.execute('UPDATE devices SET last_seen=? WHERE id=?', (time.time(), device['id']))
            return {**license_status(row, mode), 'user': self.public(row) if mode == 'account' else None}

    @staticmethod
    def desktop_redirect(value):
        try:
            url = urlsplit(value)
            if (url.scheme != 'http' or url.hostname != '127.0.0.1' or not url.port or url.username or url.password
                    or url.path != '/api/auth/desktop/callback' or url.query or url.fragment):
                fail('INVALID_DESKTOP_REDIRECT')
        except ValueError:
            fail('INVALID_DESKTOP_REDIRECT')
        return value

    def exchange(self, body):
        if set(body) - {'code', 'verifier', 'redirectUri', 'installationId', 'name'}:
            fail('INVALID_FIELDS')
        with self.connect(write=True) as db:
            code = db.execute('SELECT * FROM desktop_codes WHERE hash=?', (self.digest(str(body.get('code', ''))),)).fetchone()
            verifier = str(body.get('verifier', ''))
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
            if (not code or code['expires'] <= time.time() or not 43 <= len(verifier) <= 128
                    or not secrets.compare_digest(code['challenge'], challenge) or code['redirect_uri'] != body.get('redirectUri')):
                fail('DESKTOP_CODE_INVALID', 401)
            row = self.get(code['user_id'], db)
            if row['deleted']:
                fail('ACCOUNT_DELETED', 403)
            db.execute('DELETE FROM desktop_codes WHERE hash=?', (code['hash'],))
            return self.issue_device(db, row, body.get('installationId'), body.get('name', ''), 'account')


async def read_body(request):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 32_768:
            fail('INVALID_FIELDS', 413)
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        fail('INVALID_FIELDS', 400)
    if not isinstance(value, dict):
        fail('INVALID_FIELDS')
    return value


def management_routes(auth):
    router = APIRouter()
    rates = defaultdict(deque)
    rate_lock = threading.Lock()

    def actor(request):
        user = auth.user(request)
        if not user:
            fail('WEB_LOGIN_REQUIRED', 401)
        if request.method != 'GET' and request.headers.get('x-zm-account') != user['id']:
            fail('WEB_ACCOUNT_CHANGED', 401)
        return user

    @router.get('/api/admin/{kind}')
    def listing(kind: str, request: Request):
        if kind not in ('accounts', 'keys'):
            fail('NOT_FOUND', 404)
        return auth.list_items(actor(request)['id'], kind, dict(request.query_params))

    async def mutate(kind, uid, request, action='update'):
        if kind not in ('accounts', 'keys') or action == 'rotate' and kind != 'keys' or action == 'revoke-sessions' and kind != 'accounts':
            fail('NOT_FOUND', 404)
        data = {} if request.method == 'DELETE' else await read_body(request)
        try:
            result = await anyio.to_thread.run_sync(auth.mutate, actor(request)['id'], kind, uid, data, action)
        except sqlite3.IntegrityError:
            fail('ACCOUNT_ALREADY_EXISTS', 409)
        return result

    @router.post('/api/admin/{kind}')
    async def create(kind: str, request: Request):
        return await mutate(kind, None, request)

    @router.patch('/api/admin/{kind}/{uid}')
    async def edit(kind: str, uid: str, request: Request):
        return await mutate(kind, uid, request)

    @router.delete('/api/admin/{kind}/{uid}')
    async def delete(kind: str, uid: str, request: Request):
        return await mutate(kind, uid, request, 'delete')

    @router.post('/api/admin/{kind}/{uid}/{action}')
    async def command(kind: str, uid: str, action: str, request: Request):
        if action not in ('renew', 'revoke-sessions', 'rotate', 'restore'):
            fail('NOT_FOUND', 404)
        return await mutate(kind, uid, request, action)

    @router.get('/api/admin/keys/{uid}/devices')
    def devices(uid: str, request: Request):
        return auth.device_list(actor(request)['id'], uid)

    @router.delete('/api/admin/keys/{uid}/devices/{device_id}')
    def revoke(uid: str, device_id: str, request: Request):
        return auth.revoke_device(actor(request)['id'], uid, device_id)

    @router.get('/api/admin/resellers/options')
    def reseller_options(request: Request):
        user = actor(request)
        auth.authorize(user)
        if user['role'] == 'reseller':
            return []
        with auth.connect() as db:
            return [dict(row) for row in db.execute("SELECT id,name,email FROM users WHERE role='reseller' AND deleted=0 ORDER BY email")]

    @router.get('/api/admin/issuers/options')
    def issuer_options(request: Request):
        user = actor(request)
        auth.authorize(user)
        with auth.connect() as db:
            return [dict(row) for row in db.execute("SELECT id,name,email FROM users WHERE role!='user' AND (? OR id=?) ORDER BY email", (user['role'] != 'reseller', user['id']))]

    @router.get('/api/admin/audit/history')
    def history(request: Request):
        user = actor(request)
        auth.authorize(user)
        with auth.connect() as db:
            # Dealers see only their own management actions.
            rows = db.execute('SELECT audit.*,users.email AS actor_email FROM audit LEFT JOIN users ON users.id=audit.actor_id WHERE (? OR actor_id=?) ORDER BY audit.id DESC LIMIT 200',
                              (user['role'] in ('owner', 'admin'), user['id']))
            return [dict(row) for row in rows]

    @router.get('/api/desktop/authorize')
    def desktop_authorize(request: Request, challenge: str, redirect_uri: str, state: str):
        auth.desktop_redirect(redirect_uri)
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}', challenge) or not re.fullmatch(r'[A-Za-z0-9_-]{20,128}', state):
            fail('INVALID_DESKTOP_REQUEST')
        user = auth.user(request)
        if not user:
            return RedirectResponse('/login?' + urlencode({'return_to': str(request.url.path) + '?' + request.url.query}), status_code=303)
        code = secrets.token_urlsafe(32)
        with auth.connect(write=True) as db:
            db.execute('DELETE FROM desktop_codes WHERE expires<=?', (time.time(),))
            db.execute('INSERT INTO desktop_codes VALUES(?,?,?,?,?)', (auth.digest(code), user['id'], challenge, redirect_uri, time.time() + 60))
        return RedirectResponse(redirect_uri + '?' + urlencode({'code': code, 'state': state}), status_code=303,
                                headers={'Referrer-Policy': 'no-referrer'})

    @router.post('/api/desktop/{action}')
    async def desktop_action(action: str, request: Request):
        if action not in ('activate', 'exchange', 'status', 'logout'):
            fail('NOT_FOUND', 404)
        # ponytail: bounded in-process limiter for the one-host gateway. For multiple
        # gateways move this counter to a shared store/reverse proxy.
        address = request.client.host if request.client else ''
        now = time.monotonic()
        with rate_lock:
            for key in list(rates):
                if not rates[key] or now - rates[key][-1] >= 60:
                    del rates[key]
            bucket = rates[(address, action)]
            while bucket and now - bucket[0] >= 60:
                bucket.popleft()
            if len(bucket) >= (240 if action == 'status' else 30):
                fail('RATE_LIMITED', 429)
            bucket.append(now)
        body = await read_body(request)
        # These are public APP credentials, independent of browser cookies.
        if action == 'activate':
            return await anyio.to_thread.run_sync(auth.activate, body)
        if action == 'exchange':
            return await anyio.to_thread.run_sync(auth.exchange, body)
        return await anyio.to_thread.run_sync(auth.device_status, body.get('deviceToken'), action == 'logout')

    return router
