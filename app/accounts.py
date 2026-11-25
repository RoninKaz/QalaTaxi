"""Аккаунты, хеши паролей, сессии и одноразовые приглашения."""
import hashlib
import hmac
import re
import secrets
import time

ROLES = ('client', 'driver', 'admin')


def phone_number(value):
    value = re.sub(r'[\s()\-]', '', str(value))
    if value.startswith('8') and len(value) == 11:
        value = '+7' + value[1:]
    if not value.startswith('+'):
        value = '+' + value
    if not re.fullmatch(r'\+[1-9][0-9]{9,14}', value):
        raise ValueError('Введите номер с кодом страны, например +7 700 123 45 67.')
    return value


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password, salt=None):
    if not 8 <= len(password) <= 128:
        raise ValueError('Пароль должен содержать от 8 до 128 символов.')
    salt = salt or secrets.token_hex(16)
    result = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 600000).hex()
    return salt + ':' + result


class Accounts:
    def __init__(self, service):
        self.service = service
        self.db = service.db
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, phone TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('client','driver','admin')),
                password_hash TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(phone, role));
            CREATE UNIQUE INDEX IF NOT EXISTS driver_account ON drivers(user_id);
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY, role TEXT NOT NULL,
                user_id INTEGER REFERENCES users(id), csrf TEXT NOT NULL, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS invites (
                token_hash TEXT PRIMARY KEY, role TEXT NOT NULL, phone TEXT,
                name TEXT, car TEXT, expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0);
        ''')

    def new_session(self, role, user_id=None, old_token=None):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.db:
            self.db.execute('DELETE FROM sessions WHERE expires < ?', (time.time(),))
            if old_token:
                self.db.execute('DELETE FROM sessions WHERE token_hash=? AND role=?', (digest(old_token), role))
            self.db.execute('INSERT INTO sessions VALUES (?, ?, ?, ?, ?)',
                            (digest(token), role, user_id, csrf, time.time() + 43200))
        return token, csrf

    def session(self, token, role):
        return self.db.execute('SELECT * FROM sessions WHERE token_hash=? AND role=? AND expires>?',
                               (digest(token), role, time.time())).fetchone()

    def user(self, user_id):
        return self.db.execute('SELECT id, name, phone, role FROM users WHERE id=?', (user_id,)).fetchone()

    def driver(self, user_id):
        return next((d for d in self.service.drivers() if d['user_id'] == user_id), None)

    def invite(self, role, phone='', name='', car=''):
        if role != 'driver':
            raise ValueError('Недопустимое приглашение.')
        if role == 'driver':
            phone = phone_number(phone)
            if not name.strip() or not car.strip() or max(len(name), len(car)) > 120:
                raise ValueError('Укажите имя и автомобиль (до 120 символов).')
            if self.db.execute("SELECT id FROM users WHERE phone=? AND role='driver'", (phone,)).fetchone():
                raise ValueError('Водитель уже зарегистрирован. Проверьте список заявок.')
        token = secrets.token_urlsafe(24)
        with self.db:
            self.db.execute('INSERT INTO invites VALUES (?, ?, ?, ?, ?, ?, 0)',
                            (digest(token), role, phone, name.strip(), car.strip(), time.time() + 86400))
        return token

    def register(self, role, name, phone, password, car='', invite=''):
        if role not in ('client', 'driver'):
            raise ValueError('Администратор входит только через Google.')
        name = name.strip()
        if not 1 <= len(name) <= 120:
            raise ValueError('Укажите имя (до 120 символов).')
        phone = phone_number(phone)
        hashed = password_hash(password)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            invitation = None
            if invite:
                invitation = self.db.execute('SELECT * FROM invites WHERE token_hash=? AND role=? AND used=0 AND expires>?',
                                             (digest(invite), role, time.time())).fetchone()
                if invitation is None or invitation['phone'] and invitation['phone'] != phone:
                    raise ValueError('Приглашение недействительно, истекло или выдано другому номеру.')
            if role == 'driver':
                car = invitation['car'] if invitation else car.strip()
                if not 1 <= len(car) <= 120:
                    raise ValueError('Укажите модель и госномер автомобиля (до 120 символов).')
            if self.db.execute('SELECT id FROM users WHERE phone=? AND role=?', (phone, role)).fetchone():
                raise ValueError('Аккаунт с этим номером уже существует. Войдите в него.')
            cursor = self.db.execute('INSERT INTO users(name, phone, role, password_hash, created_at) VALUES (?, ?, ?, ?, ?)',
                                     (name, phone, role, hashed, self.service.now()))
            user_id = cursor.lastrowid
            if role == 'driver':
                self.db.execute('INSERT INTO drivers(name, car, user_id, approval) VALUES (?, ?, ?, ?)',
                                 (name, car, user_id, 'approved'))
            if invitation:
                self.db.execute('UPDATE invites SET used=1 WHERE token_hash=?', (digest(invite),))
        return self.user(user_id)

    def login(self, role, phone, password):
        if role not in ('client', 'driver'):
            raise ValueError('Администратор входит только через Google.')
        phone = phone_number(phone)
        row = self.db.execute('SELECT * FROM users WHERE phone=? AND role=?', (phone, role)).fetchone()
        google_only = row is not None and row['password_hash'] == '!google-only'
        salt = row['password_hash'].split(':')[0] if row and not google_only else '00' * 16
        try:
            candidate = password_hash(password, salt)
        except ValueError:
            raise ValueError('Неверный номер или пароль.') from None
        if row is None or google_only or not hmac.compare_digest(candidate, row['password_hash']):
            raise ValueError('Неверный номер или пароль.')
        return self.user(row['id'])

    def complete_driver_profile(self, user_id, phone, car):
        phone = phone_number(phone)
        car = car.strip()
        if not 1 <= len(car) <= 120:
            raise ValueError('Укажите модель и госномер автомобиля (до 120 символов).')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            driver = self.db.execute("SELECT id FROM drivers WHERE user_id=? AND approval='draft'", (user_id,)).fetchone()
            if driver is None:
                raise ValueError('Анкета уже отправлена.')
            if self.db.execute("SELECT id FROM users WHERE role='driver' AND phone=? AND id!=?", (phone, user_id)).fetchone():
                raise ValueError('Этот номер уже привязан к другому кабинету водителя. Войдите в существующий аккаунт.')
            self.db.execute('UPDATE users SET phone=? WHERE id=?', (phone, user_id))
            self.db.execute("UPDATE drivers SET car=?, approval='approved' WHERE id=?", (car, driver['id']))
