"""Веб-приложение Qala Taxi: python main.py. Python 3.10+, без pip."""
import argparse
import html
import os
import secrets
import sqlite3
import time
import threading
from http.cookies import SimpleCookie
from contextlib import closing
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from app.service import TaxiService, TARIFFS, default_database
from app.accounts import Accounts, ROLES
from app import google_auth

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / 'static'
SECURE_COOKIES = os.environ.get('COOKIE_SECURE') == '1'


def esc(value):
    return html.escape(str(value), quote=True)


def money(value):
    return f'{Decimal(str(value)):,.2f}'.replace(',', ' ') + ' ₸'


def field(label, name, value='', kind='text', extra=''):
    return f'<label>{label}<input name="{name}" type="{kind}" value="{esc(value)}" {extra}></label>'


def hidden(name, value):
    return f'<input type="hidden" name="{name}" value="{esc(value)}">'


def form(action, token, body, back):
    return f'<form method="post" action="/{action}">{hidden("csrf", token)}{hidden("back", back)}{body}</form>'


def button(text, secondary=False):
    return f'<button class="button {"secondary" if secondary else ""}">{text}</button>'


def status(value):
    colors = {'Новый': 'new', 'Назначен': 'assigned', 'В пути': 'moving', 'Завершён': 'done'}
    return f'<span class="badge {colors[value]}">{value}</span>'


def orders_table(rows):
    if not rows:
        return '<div class="empty"><div class="empty-icon">◇</div><h3>Здесь появятся поездки</h3><p>Оформите первый заказ в панели клиента.</p></div>'
    body = ''.join(f'''<tr><td><strong>#{r['id']:03}</strong></td>
        <td>{esc(r['client'])}<small>{esc(r['tariff'])}</small></td>
        <td>{esc(r['origin'])}<small>до {esc(r['destination'])}</small></td>
        <td>{r['distance']:g} км</td><td>{money(Decimal(r['total_cents']) / 100)}
        <small>Скидка {r['discount']:g}% · ожидание {r['waiting']} мин</small></td>
        <td>{status(r['status'])}</td><td>{esc(r['driver_name'] or 'Ожидается')}</td></tr>''' for r in rows)
    return '<div class="table-scroll"><table><thead><tr><th>Заказ</th><th>Клиент</th><th>Маршрут</th><th>Расстояние</th><th>Стоимость</th><th>Статус</th><th>Водитель</th></tr></thead><tbody>' + body + '</tbody></table></div>'


def client_page(service, token, values, user):
    selected = values.get('tariff', 'Эконом')
    tariffs = ''.join(f'''<label class="tariff"><input type="radio" name="tariff" value="{name}" {'checked' if selected == name else ''} required>
        <span><b>{name}</b><small>{rate} ₸ / км</small></span></label>''' for name, rate in TARIFFS.items())
    content = f'<p class="welcome">Здравствуйте, {esc(user["name"])}!</p>'
    content += '<div class="two">' + field('Откуда', 'origin', values.get('origin', ''), extra='required maxlength="120" placeholder="Улица и номер дома"')
    content += field('Куда', 'destination', values.get('destination', ''), extra='required maxlength="120" placeholder="Адрес назначения"') + '</div>'
    content += field('Расстояние, км', 'distance', values.get('distance', '5'), extra='required inputmode="decimal" placeholder="Например, 5,5"')
    content += '<label>Выберите тариф</label><div class="tariffs">' + tariffs + '</div>'
    content += '<div class="form-actions"><button class="button secondary" formaction="/quote">Рассчитать</button>' + button('Заказать такси') + '</div>'
    quote = ''
    if values.get('distance'):
        try:
            amount = service.calculator.taxiCost(values['distance'], TARIFFS[selected])
            quote = f'<div class="estimate"><span>Предварительная стоимость</span><strong>{money(amount)}</strong><small>Без ожидания и скидки</small></div>'
        except (ValueError, KeyError):
            pass
    return f'''<div class="booking-layout"><section class="card booking"><div class="section-heading"><h2>Новая поездка</h2><span class="muted">Просто и по делу</span></div>
        {form('order', token, content, '/client')}</section>
        <aside class="trip-preview"><div class="map-art" aria-hidden="true"><div class="park p1"></div><div class="park p2"></div><div class="road r1"></div><div class="road r2"></div><div class="road r3"></div><div class="route-line"></div><div class="pin a">A</div><div class="pin b">B</div><div class="map-caption">ВАШ ГОРОД. ВАШ МАРШРУТ.</div></div>
        <div class="preview-body"><span class="eyebrow">ПОЕЗДКИ БЕЗ ЛИШНЕГО</span><h2>Хороший день<br>начинается с поездки.</h2><p>Выберите маршрут и тариф.<br>Остальное — за нашей командой.</p>{quote or '<div class="estimate"><span>Эконом · от 120 ₸ / км</span><small>Нажмите «Рассчитать», чтобы узнать сумму.</small></div>'}
        <div class="price-note"><b>40 ₸ / мин</b><span>Ожидание добавляет водитель.<br>Скидку назначает администратор.</span></div></div></aside></div>
        <section class="card"><div class="section-heading"><h2>Мои заказы</h2><span class="muted">Ваши поездки</span></div>{orders_table(service.orders(client_id=user['id']))}</section>'''


def driver_page(service, token, params, user):
    driver = Accounts(service).driver(user['id'])
    if driver is None:
        raise ValueError('Профиль водителя не найден.')
    if driver['approval'] == 'draft':
        fields = field('Номер телефона', 'phone', kind='tel', extra='required placeholder="+7 700 123 45 67"') + field('Модель и госномер автомобиля', 'car', extra='required maxlength="120"') + button('Сохранить и начать работу')
        return '<section class="card"><h2>Завершите регистрацию водителя</h2><p>Укажите контактный номер и автомобиль. После сохранения вы сможете работать с поездками.</p>' + form('driver-profile', token, fields, '/driver') + '</section>'
    driver_id = driver['id']
    back = '/driver'
    selector = f'<div class="profile"><div class="avatar">{esc(user["name"][0])}</div><div><b>{esc(user["name"])}</b><p>{esc(driver["car"])} · {esc(user["phone"])}</p></div></div>'
    rows = service.orders(driver_id)
    active = [r for r in rows if r['status'] != 'Завершён']
    trips = ''
    for r in active:
        ids = hidden('order_id', r['id']) + hidden('driver_id', driver_id)
        if r['status'] == 'Назначен':
            controls = form('start', token, ids + button('Начать поездку'), back)
        else:
            controls = form('complete', token, ids + field('Ожидание, минут', 'waiting', '0', 'number', 'required min="0" max="1440" step="1"') + button('Завершить поездку'), back)
        trips += f'''<article class="card active-trip"><div class="section-heading"><h2>Поездка #{r['id']:03}</h2>{status(r['status'])}</div>
            <div class="route"><span>A</span><div>{esc(r['origin'])}</div><span>B</span><div>{esc(r['destination'])}</div></div>
            <p>{esc(r['client'])} · {r['distance']:g} км · {esc(r['tariff'])}</p><h2>{money(Decimal(r['total_cents']) / 100)}</h2><p class="muted">Скидка: {r['discount']:g}%. Ожидание будет добавлено при завершении.</p>{controls}</article>'''
    return f'''<section class="card"><div class="section-heading"><h2>Ваш профиль</h2><span class="badge {'assigned' if driver['busy'] else 'done'}">{'На заказе' if driver['busy'] else 'Свободен'}</span></div>{selector}</section>
        {trips or '<section class="card empty"><div class="empty-icon">✓</div><h2>Вы готовы к новой поездке</h2><p>Назначенный администратором заказ появится здесь.</p></section>'}
        <section class="card"><div class="section-heading"><h2>Мои поездки</h2><a href="{esc(back)}">Обновить</a></div>{orders_table(rows)}</section>'''


def admin_page(service, token, params, user):
    from datetime import date
    day = params.get('day') or date.today().isoformat()
    try:
        date.fromisoformat(day)
    except ValueError:
        raise ValueError('Укажите корректную дату.') from None
    stats = service.statistics(day)
    longest = stats['longest']
    cards = [('Доход за день', money(stats['income']), f'{stats["count"]} завершённых поездок'),
             ('Активные заказы', str(stats['active']), 'Сейчас в работе и ожидании'),
             ('Самая длинная поездка', f'{longest["distance"]:g} км' if longest else '—', f'Заказ #{longest["id"]:03} · за всё время' if longest else 'Пока нет завершённых поездок')]
    metrics = '<div class="metrics">' + ''.join(f'<div class="card metric"><span>{a}</span><strong>{b}</strong><small>{c}</small></div>' for a, b, c in cards) + '</div>'
    rows = service.orders()
    active = [r for r in rows if r['status'] != 'Завершён']
    new = [r for r in rows if r['status'] == 'Новый']
    free = [d for d in service.drivers() if not d['busy'] and d['approval'] == 'approved']
    def order_options(items):
        return ''.join(f'<option value="{r["id"]}">#{r["id"]:03} · {esc(r["client"])} · {esc(r["origin"])}</option>' for r in items)
    assign = '<p class="muted">Для назначения нужны новый заказ и свободный водитель.</p>'
    if new and free:
        options = ''.join(f'<option value="{d["id"]}">{esc(d["name"])} · {esc(d["car"])}</option>' for d in free)
        assign = form('assign', token, f'<label>Новый заказ<select name="order_id">{order_options(new)}</select></label><label>Свободный водитель<select name="driver_id">{options}</select></label>' + button('Назначить водителя'), '/admin')
    discount = '<p class="muted">Скидка доступна для незавершённых заказов.</p>'
    if active:
        discount = form('discount', token, f'<label>Заказ<select name="order_id">{order_options(active)}</select></label>' + field('Скидка, %', 'percent', '10', 'number', 'required min="0" max="50" step="0.1"') + button('Применить скидку'), '/admin')
    fleet = ''.join(f'<div class="driver-row"><div class="avatar">{esc(d["name"][0])}</div><div><b>{esc(d["name"])}</b><small>{esc(d["car"])}</small></div><span class="badge {"assigned" if d["busy"] else "done"}">{"На заказе" if d["busy"] else "Свободен"}</span></div>' for d in service.drivers() if d['approval'] == 'approved') or '<p class="muted">Зарегистрированных водителей пока нет.</p>'
    invite_fields = field('Имя водителя', 'name', extra='required maxlength="120"') + field('Номер телефона', 'phone', kind='tel', extra='required placeholder="+7 700 123 45 67"') + field('Модель и госномер автомобиля', 'car', extra='required maxlength="120"') + button('Создать приглашение')
    management = f'<section class="card"><h2>Добавить нового водителя</h2><p class="muted">Создайте приглашение. Водитель сам задаст пароль и сможет начать работу. Ссылка действует 24 часа.</p>{form("invite-driver", token, invite_fields, "/admin")}</section>'
    return f'''<form method="get" class="date-filter" action="/admin">{field('Дата отчёта', 'day', day, 'date', 'required')}{button('Показать', True)}</form>{metrics}
        <div class="two admin-actions"><section class="card"><h2>Назначение водителя</h2>{assign}</section><section class="card"><h2>Скидка на поездку</h2>{discount}</section></div>
        <section class="card"><div class="section-heading"><h2>Все заказы</h2><a href="/admin">Обновить</a></div>{orders_table(rows)}</section>
        <section class="card"><h2>Водители</h2><div class="fleet">{fleet}</div></section>{management}'''



def shell(title, content, user=None, token='', error='', message=''):
    account = ''
    if user:
        labels = {'client': 'Клиент', 'driver': 'Водитель', 'admin': 'Администратор'}
        account = f'<div class="account-bar"><span>{esc(user["name"])} · {labels[user["role"]]}</span>{form("logout/" + user["role"], token, button("Выйти", True), "/")}</div>'
    alert = f'<div class="alert {"error" if error else ""}" role="status">{esc(error or message)}</div>' if error or message else ''
    home = '/' + user['role'] if user else '/'
    return f'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Qala Taxi — {esc(title)}</title><link rel="stylesheet" href="/style.css"></head>
        <body><div class="topbar"><a class="brand" href="{home}" aria-label="Qala Taxi"><span class="brand-icon">Q</span>qala<span class="brand-light">taxi</span></a></div>
        <main class="app-main">{account}{alert}{content}</main></body></html>'''


def landing():
    cards = ''
    for role, title, text in [('client', 'Для клиентов', 'Заказывайте поездки и следите за их статусом.'),
                              ('driver', 'Для водителей', 'Ваши заказы, маршруты и история поездок.')]:
        cards += f'<article class="card portal"><h2>{title}</h2><p>{text}</p><a class="button" target="_blank" rel="noopener" href="/login/{role}">Открыть кабинет</a></article>'
    return shell('Добро пожаловать', f'<section class="portal-intro"><span class="eyebrow">ВАШ ГОРОД. ВАШ МАРШРУТ.</span><h1>Поехали вместе.</h1><p>Выберите свой кабинет. Он откроется в отдельной вкладке.</p></section><div class="portals">{cards}</div>')


def auth_page(role, mode, token, values=None, error=''):
    if role == 'admin':
        return google_login_page(token, error)
    values = values or {}
    labels = {'client': 'клиента', 'driver': 'водителя', 'admin': 'администратора'}
    register = mode == 'register'
    title = ('Регистрация ' if register else 'Вход в кабинет ') + labels[role]
    fields = ''
    if register:
        fields += field('Ваше имя', 'name', values.get('name', ''), extra='required maxlength="120" autocomplete="name"')
    fields += field('Номер телефона', 'phone', values.get('phone', ''), 'tel', 'required maxlength="24" autocomplete="tel" placeholder="+7 700 123 45 67"')
    fields += field('Пароль', 'password', kind='password', extra=f'required minlength="8" maxlength="128" autocomplete="{"new-password" if register else "current-password"}"')
    if register:
        fields += field('Повторите пароль', 'confirm', kind='password', extra='required minlength="8" maxlength="128" autocomplete="new-password"')
        if role == 'driver':
            fields += field('Модель и госномер автомобиля', 'car', values.get('car', ''), extra='maxlength="120" placeholder="Например, Toyota Camry · 123 ABC"')
            fields += '<p class="auth-note">После регистрации можно сразу начать работу.</p>'
        if role in ('driver', 'admin'):
            fields += field('Код приглашения' + (' (если есть)' if role == 'driver' else ' владельца'), 'invite', values.get('invite', ''), extra='maxlength="100"' + (' required' if role == 'admin' else ''))
            if role == 'admin':
                fields += '<p class="auth-note">Одноразовый код создаёт владелец приложения на компьютере сервера.</p>'
    fields += button('Создать аккаунт' if register else 'Войти')
    switch = f'<a href="/login/{role}">Уже есть аккаунт? Войти</a>' if register else f'<a href="/register/{role}">Создать аккаунт</a>'
    google = form('auth/google/start/' + role, token, button('Продолжить с Google', True), '/login/' + role) if google_auth.ready(google_auth.load_config(), role) else ''
    content = f'<div class="auth-wrap"><section class="card auth-card"><h1>{title}</h1>{google}<p class="muted">Или по номеру телефона и паролю</p>{form(mode + "/" + role, token, fields, "/login/" + role)}<div class="auth-links">{switch}<a href="/">Все кабинеты</a></div></section></div>'
    return shell(title, content, error=error)


def google_login_page(token, error=''):
    config = google_auth.load_config()
    if google_auth.ready(config):
        control = form('auth/google/start', token, button('Войти через Google'), '/login/admin')
        note = 'Аккаунт создаётся автоматически при первом входе.'
    else:
        control = '<button class="button secondary" disabled>Войти через Google</button>'
        note = 'Вход через Google пока не подключён. Владелец сервиса должен завершить настройку.'
    return shell('Вход для управления сервисом', f'<div class="auth-wrap"><section class="card auth-card"><h1>Управление сервисом</h1><p class="auth-note">{note}</p>{control}</section></div>', error=error)


def page(service, token, path, params, user, error=''):
    panel = user['role']
    titles = {'client': ('Куда поедем сегодня?', 'Ваш маршрут начинается здесь.'),
              'driver': ('Хорошей дороги!', 'Все ваши поездки — под рукой.'),
              'admin': ('Город под контролем', 'Управляйте заказами и следите за результатами.')}
    title, subtitle = titles[panel]
    content = {'client': client_page, 'driver': driver_page, 'admin': admin_page}[panel](service, token, params, user)
    content = f'<header><div><h1>{title}</h1><p>{subtitle}</p></div></header>' + content
    return shell(title, content, user, token, error, params.get('message', ''))


class Handler(BaseHTTPRequestHandler):
    def respond(self, code, body, content_type='text/html; charset=utf-8', cookie=None):
        data = body.encode('utf-8') if isinstance(body, str) else body
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self'; form-action 'self' https://accounts.google.com; frame-ancestors 'none'; base-uri 'none'")
        if cookie:
            self.send_header('Set-Cookie', cookie)
        self.end_headers()
        self.wfile.write(data)

    def cookie(self, role, token, clear=False):
        return f'qala_{role}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={0 if clear else 43200}' + ('; Secure' if SECURE_COOKIES else '')

    def read_session(self, accounts, role):
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get('Cookie', ''))
        except Exception:
            return None, ''
        item = cookies.get('qala_' + role)
        token = item.value if item else ''
        return accounts.session(token, role) if token else None, token

    def redirect(self, path, cookie=None):
        self.send_response(303)
        self.send_header('Location', path)
        self.send_header('Cache-Control', 'no-store')
        if cookie:
            self.send_header('Set-Cookie', cookie)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == '/style.css':
            return self.respond(200, (STATIC / 'style.css').read_bytes(), 'text/css; charset=utf-8')
        if url.path == '/':
            return self.respond(200, landing())
        params = {key: values[0] for key, values in parse_qs(url.query).items()}
        parts = url.path.strip('/').split('/')
        with closing(TaxiService(self.server.database)) as service:
            accounts = Accounts(service)
            if url.path == '/auth/google/callback':
                google_auth.setup(service.db)
                pending = service.db.execute('SELECT role FROM oauth_requests WHERE state=?', (params.get('state', ''),)).fetchone()
                role = pending['role'] if pending else 'admin'
                session, old_token = self.read_session(accounts, role)
                if session is None:
                    return self.redirect('/login/' + role)
                try:
                    if params.get('error'):
                        raise ValueError('Вход через Google отменён. Можно попробовать снова.')
                    user = google_auth.finish(accounts, params.get('state', ''), params.get('code', ''), session['token_hash'], google_auth.load_config(), role)
                    token, _ = accounts.new_session(role, user['id'], old_token)
                    return self.redirect('/' + role, self.cookie(role, token))
                except ValueError as error:
                    return self.respond(400, auth_page(role, 'login', session['csrf'], error=str(error)))
            if url.path == '/register/admin':
                return self.redirect('/login/admin')
            if len(parts) == 2 and parts[0] in ('login', 'register') and parts[1] in ROLES:
                mode, role = parts
                session, old_token = self.read_session(accounts, role)
                if session and session['user_id'] and (role != 'admin' or google_auth.authorized_user(accounts, session['user_id'], google_auth.load_config())):
                    return self.redirect('/' + role)
                cookie = None
                if session is None:
                    token, csrf = accounts.new_session(role)
                    cookie = self.cookie(role, token)
                else:
                    csrf = session['csrf']
                return self.respond(200, auth_page(role, mode, csrf, params), cookie=cookie)
            if len(parts) != 1 or parts[0] not in ROLES:
                return self.respond(404, shell('Не найдено', '<section class="card"><h1>Страница не найдена</h1><a href="/">На главную</a></section>'))
            role = parts[0]
            session, _ = self.read_session(accounts, role)
            if session is None or session['user_id'] is None:
                return self.redirect('/login/' + role)
            user = accounts.user(session['user_id'])
            if role == 'admin' and not google_auth.authorized_user(accounts, user['id'], google_auth.load_config()):
                return self.redirect('/login/admin', self.cookie('admin', '', True))
            try:
                self.respond(200, page(service, session['csrf'], url.path, params, user))
            except ValueError as error:
                self.respond(400, shell('Ошибка', '<a href="/' + role + '">Вернуться в кабинет</a>', error=str(error)))

    def do_POST(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 16384:
                return self.respond(413, 'Слишком большой или пустой запрос')
            values = {key: val[0] for key, val in parse_qs(self.rfile.read(length).decode('utf-8'), keep_blank_values=True).items()}
        except (ValueError, UnicodeDecodeError):
            return self.respond(400, 'Некорректный запрос')
        path = urlsplit(self.path).path.strip('/')
        parts = path.split('/')
        if path in ('login/admin', 'register/admin'):
            return self.respond(403, shell('Вход через Google', '<p>Вход по телефону для администратора отключён.</p><a href="/login/admin">Войти через Google</a>'))
        auth_action = len(parts) == 2 and parts[0] in ('login', 'register', 'logout') and parts[1] in ROLES
        roles = {'quote': 'client', 'order': 'client', 'start': 'driver', 'complete': 'driver',
                 'assign': 'admin', 'discount': 'admin', 'invite-driver': 'admin', 'auth/google/start': 'admin',
                 'auth/google/start/client': 'client', 'auth/google/start/driver': 'driver', 'driver-profile': 'driver'}
        role = parts[1] if auth_action else roles.get(path)
        if role is None:
            return self.respond(404, 'Действие не найдено')
        with closing(TaxiService(self.server.database)) as service:
            accounts = Accounts(service)
            session, old_token = self.read_session(accounts, role)
            if session is None or not secrets.compare_digest(values.get('csrf', ''), session['csrf']):
                return self.respond(403, shell('Сессия истекла', f'<section class="card"><h2>Обновите страницу и повторите действие.</h2><a href="/login/{role}">Перейти ко входу</a></section>'))
            user = accounts.user(session['user_id']) if session['user_id'] else None
            if path in ('auth/google/start', 'auth/google/start/client', 'auth/google/start/driver'):
                try:
                    return self.redirect(google_auth.start(accounts.db, session['token_hash'], google_auth.load_config(), role))
                except ValueError as error:
                    return self.respond(503, auth_page(role, 'login', session['csrf'], error=str(error)))
            if auth_action and parts[0] == 'logout':
                with service.db:
                    service.db.execute('DELETE FROM sessions WHERE token_hash=?', (session['token_hash'],))
                return self.redirect('/login/' + role, self.cookie(role, '', True))
            if auth_action:
                if user:
                    return self.redirect('/' + role)
                # Лимит на попытки входа/регистрации для адреса и роли.
                key = (self.client_address[0], role)
                with self.server.attempt_lock:
                    now = time.time()
                    attempts = [t for t in self.server.attempts.get(key, []) if now - t < 900]
                    self.server.attempts[key] = attempts
                    if len(attempts) >= 15:
                        return self.respond(429, auth_page(role, parts[0], session['csrf'], values, 'Слишком много попыток. Повторите через 15 минут.'))
                    attempts.append(now)
                try:
                    if parts[0] == 'register':
                        if values.get('password') != values.get('confirm'):
                            raise ValueError('Пароли не совпадают.')
                        user = accounts.register(role, values.get('name', ''), values.get('phone', ''), values.get('password', ''), values.get('car', ''), values.get('invite', ''))
                    else:
                        user = accounts.login(role, values.get('phone', ''), values.get('password', ''))
                    with self.server.attempt_lock:
                        self.server.attempts.pop(key, None)
                    token, _ = accounts.new_session(role, user['id'], old_token)
                    return self.redirect('/' + role, self.cookie(role, token))
                except ValueError as error:
                    return self.respond(400, auth_page(role, parts[0], session['csrf'], values, str(error)))
            if user is None or user['role'] != role:
                return self.respond(403, shell('Нет доступа', '<h1>Войдите в свой кабинет.</h1>'))
            if role == 'admin' and not google_auth.authorized_user(accounts, user['id'], google_auth.load_config()):
                return self.respond(403, google_login_page(session['csrf'], 'Доступ администратора не разрешён.'))
            try:
                if path == 'driver-profile':
                    accounts.complete_driver_profile(user['id'], values.get('phone', ''), values.get('car', ''))
                    return self.redirect('/driver')
                if role == 'driver':
                    driver = accounts.driver(user['id'])
                    if driver is None or driver['approval'] != 'approved':
                        return self.respond(403, page(service, session['csrf'], '/driver', {}, user, 'Сначала заполните телефон и автомобиль в профиле.'))
                if path == 'quote':
                    if values.get('tariff') not in TARIFFS:
                        raise ValueError('Выберите тариф.')
                    service.calculator.taxiCost(values.get('distance'), TARIFFS[values['tariff']])
                    return self.respond(200, page(service, session['csrf'], '/client', values, user))
                if path == 'order':
                    oid = service.create_order(user['name'], values.get('origin', ''), values.get('destination', ''), values.get('distance', ''), values.get('tariff', ''), client_id=user['id'])
                    message = f'Заказ #{oid:03} принят. Ожидайте назначения водителя.'
                elif path == 'assign':
                    service.assign_driver(int(values['order_id']), int(values['driver_id']))
                    message = 'Водитель назначен. Заказ появился в его панели.'
                elif path == 'discount':
                    service.set_discount(int(values['order_id']), values['percent'])
                    message = 'Скидка сохранена. Стоимость пересчитана.'
                elif path == 'start':
                    service.start_trip(int(values['order_id']), driver['id'])
                    message = 'Поездка началась. Хорошей дороги!'
                elif path == 'complete':
                    quote = service.complete_trip(int(values['order_id']), driver['id'], values['waiting'])
                    message = f'Поездка завершена. Проезд: {money(quote["ride"])}. Ожидание: {money(quote["waiting"])}. Скидка: {money(quote["discount"])}. Итого: {money(quote["total"])}.'
                elif path == 'invite-driver':
                    invite = accounts.invite('driver', values.get('phone', ''), values.get('name', ''), values.get('car', ''))
                    link = '/register/driver?' + urlencode({'invite': invite, 'phone': values['phone'], 'name': values['name'], 'car': values['car']})
                    # Не сохраняем открытый код в журнале или URL административной страницы.
                    body = f'<section class="card"><h1>Приглашение создано</h1><p>Передайте этот код водителю. Он действует 24 часа, один раз, только для указанного номера.</p><label>Код приглашения<input readonly value="{esc(invite)}"></label><a class="button" href="{esc(link)}" target="_blank" rel="noopener">Открыть регистрацию водителя</a><p><a href="/admin">Вернуться в панель</a></p></section>'
                    return self.respond(200, shell('Приглашение водителя', body, user, session['csrf']))
                self.redirect('/' + role + '?' + urlencode({'message': message}))
            except (ValueError, KeyError) as error:
                message = str(error) if isinstance(error, ValueError) else 'Заполните все поля.'
                self.respond(400, page(service, session['csrf'], '/' + role, values if role == 'client' else {}, user, message))
            except sqlite3.Error:
                self.respond(503, shell('Ошибка', '<p>База данных временно недоступна. Повторите позже.</p>'))

    def log_message(self, format, *args):
        # URL приглашения и данные формы не попадают в журнал сервера.
        pass


def make_server(port=8000, database=None, host='127.0.0.1'):
    server = ThreadingHTTPServer((host, port), Handler)
    server.database = database or default_database()
    server.attempts = {}
    server.attempt_lock = threading.Lock()
    with closing(TaxiService(server.database)) as service:
        Accounts(service)
    return server


def main():
    parser = argparse.ArgumentParser(description='Qala Taxi — веб-сервер')
    # На Render порт задаётся переменной PORT, а слушать нужно все адреса.
    on_server = bool(os.environ.get('PORT'))
    parser.add_argument('--port', type=int, default=int(os.environ.get('PORT') or 8000))
    parser.add_argument('--host', default='0.0.0.0' if on_server else '127.0.0.1')
    args = parser.parse_args()
    try:
        server = make_server(args.port, host=args.host)
    except OSError as error:
        parser.exit(1, f'Не удалось запустить сервер: {error}\nПопробуйте: python main.py --port 8001\n')
    print(f'Qala Taxi: http://{args.host}:{server.server_port}\nОстановка: Ctrl+C', flush=True)
    if not google_auth.ready(google_auth.load_config()):
        print('Google для администратора не настроен: см. docs/GOOGLE_SETUP.txt.', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
