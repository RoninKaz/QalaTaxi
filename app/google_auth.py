"""Google OAuth: код авторизации + PKCE, профиль только с HTTPS API Google."""
import base64
import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

CONFIG = Path(__file__).resolve().parent.parent / 'google_oauth.json'


def load_config():
    """Файл google_oauth.json, поверх него переменные окружения (для Render)."""
    try:
        config = json.loads(CONFIG.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        config = {}
    if not isinstance(config, dict):
        config = {}
    for key, name in (('client_id', 'GOOGLE_CLIENT_ID'), ('client_secret', 'GOOGLE_CLIENT_SECRET'),
                      ('redirect_uri', 'GOOGLE_REDIRECT_URI')):
        if os.environ.get(name):
            config[key] = os.environ[name].strip()
    if os.environ.get('ADMIN_EMAILS'):
        config['admin_emails'] = [e.strip() for e in os.environ['ADMIN_EMAILS'].split(',') if e.strip()]
    return config


def ready(config, role='admin'):
    return bool(config.get('client_id') and config.get('client_secret') and config.get('redirect_uri')
                and (role != 'admin' or config.get('admin_emails')))


def setup(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS google_admins (
            subject TEXT PRIMARY KEY, user_id INTEGER UNIQUE NOT NULL REFERENCES users(id), email TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS oauth_requests (
            state TEXT PRIMARY KEY, session_hash TEXT NOT NULL, verifier TEXT NOT NULL, expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS google_accounts (
            subject TEXT NOT NULL, role TEXT NOT NULL, user_id INTEGER UNIQUE NOT NULL REFERENCES users(id),
            email TEXT NOT NULL, PRIMARY KEY(subject, role));
    ''')
    with db:
        db.execute('BEGIN IMMEDIATE')
        if 'role' not in {r['name'] for r in db.execute('PRAGMA table_info(oauth_requests)')}:
            db.execute("ALTER TABLE oauth_requests ADD COLUMN role TEXT NOT NULL DEFAULT 'admin'")


def start(db, session_hash, config, role='admin'):
    if role not in ('client', 'driver', 'admin') or not ready(config, role):
        raise ValueError('Вход через Google ещё не настроен владельцем сервиса.')
    setup(db)
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    with db:
        db.execute('DELETE FROM oauth_requests WHERE expires < ? OR session_hash=?', (time.time(), session_hash))
        db.execute('INSERT INTO oauth_requests(state,session_hash,verifier,expires,role) VALUES (?,?,?,?,?)', (state, session_hash, verifier, time.time() + 600, role))
    return 'https://accounts.google.com/o/oauth2/v2/auth?' + urlencode({
        'client_id': config['client_id'], 'redirect_uri': config['redirect_uri'],
        'response_type': 'code', 'scope': 'openid email profile', 'state': state,
        'code_challenge': challenge, 'code_challenge_method': 'S256', 'prompt': 'select_account'})


def exchange(code, verifier, config):
    payload = urlencode({'code': code, 'client_id': config['client_id'],
                         'client_secret': config['client_secret'], 'redirect_uri': config['redirect_uri'],
                         'grant_type': 'authorization_code', 'code_verifier': verifier}).encode()
    with urlopen(Request('https://oauth2.googleapis.com/token', data=payload,
                         headers={'Content-Type': 'application/x-www-form-urlencoded'}), timeout=15) as response:
        tokens = json.load(response)
    # Профиль запрашивается сервером, клиент не может прислать свой email/sub.
    request = Request('https://openidconnect.googleapis.com/v1/userinfo',
                      headers={'Authorization': 'Bearer ' + tokens['access_token']})
    with urlopen(request, timeout=15) as response:
        return json.load(response)


def allowed(profile, config):
    email = str(profile.get('email', '')).strip().lower()
    emails = config.get('admin_emails', [])
    if not isinstance(emails, list):
        return False
    # Google должен подтверждать владение адресом (Gmail или Workspace).
    return (profile.get('email_verified') is True and bool(profile.get('sub'))
            and (email.endswith('@gmail.com') or bool(profile.get('hd')))
            and email in {str(e).strip().lower() for e in emails})


def finish(accounts, state, code, session_hash, config, role='admin'):
    setup(accounts.db)
    with accounts.db:
        accounts.db.execute('BEGIN IMMEDIATE')
        request = accounts.db.execute('SELECT * FROM oauth_requests WHERE state=? AND session_hash=? AND expires>? AND role=?',
                                      (state, session_hash, time.time(), role)).fetchone()
        if request is None:
            raise ValueError('Ссылка входа истекла или уже использована. Начните вход заново.')
        accounts.db.execute('DELETE FROM oauth_requests WHERE state=?', (state,))
    try:
        profile = exchange(code, request['verifier'], config)
    except Exception:
        raise ValueError('Не удалось подтвердить вход у Google. Повторите попытку.') from None
    if role != 'admin':
        if role not in ('client', 'driver') or profile.get('email_verified') is not True or not profile.get('sub') or not profile.get('email'):
            raise ValueError('Google не подтвердил аккаунт. Выберите подтверждённый аккаунт.')
        return provision_public(accounts, profile, role)
    if not allowed(profile, config):
        raise ValueError('Этот Google-аккаунт не имеет доступа администратора.')
    return provision(accounts, profile)


def provision_public(accounts, profile, role):
    setup(accounts.db)
    with accounts.db:
        accounts.db.execute('BEGIN IMMEDIATE')
        row = accounts.db.execute('SELECT user_id FROM google_accounts WHERE subject=? AND role=?', (profile['sub'], role)).fetchone()
        if row:
            user_id = row['user_id']
        else:
            name = str(profile.get('name') or profile['email'])[:120]
            result = accounts.db.execute('INSERT INTO users(name,phone,role,password_hash,created_at) VALUES (?,?,?,?,?)',
                                        (name, 'google:' + profile['sub'], role, '!google-only', accounts.service.now()))
            user_id = result.lastrowid
            accounts.db.execute('INSERT INTO google_accounts VALUES (?,?,?,?)', (profile['sub'], role, user_id, profile['email']))
            if role == 'driver':
                accounts.db.execute("INSERT INTO drivers(name,car,user_id,approval) VALUES (?,'',?,'draft')", (name, user_id))
    return accounts.user(user_id)


def provision(accounts, profile):
    """Вызывается только после проверки ответа Google и списка доступа."""
    setup(accounts.db)
    email, subject = profile['email'].lower(), str(profile['sub'])
    with accounts.db:
        accounts.db.execute('BEGIN IMMEDIATE')
        existing = accounts.db.execute('SELECT user_id FROM google_admins WHERE subject=?', (subject,)).fetchone()
        if existing:
            user_id = existing['user_id']
            accounts.db.execute('UPDATE google_admins SET email=? WHERE subject=?', (email, subject))
        else:
            cursor = accounts.db.execute("INSERT INTO users(name,phone,role,password_hash,created_at) VALUES (?,?,'admin',?,?)",
                                         (str(profile.get('name') or email)[:120], 'google:' + subject,
                                          '!google-only', accounts.service.now()))
            user_id = cursor.lastrowid
            accounts.db.execute('INSERT INTO google_admins VALUES (?,?,?)', (subject, user_id, email))
    return accounts.user(user_id)


def authorized_user(accounts, user_id, config):
    setup(accounts.db)
    row = accounts.db.execute('SELECT email FROM google_admins WHERE user_id=?', (user_id,)).fetchone()
    emails = config.get('admin_emails', [])
    return bool(row and isinstance(emails, list) and row['email'].lower() in {str(e).strip().lower() for e in emails})
