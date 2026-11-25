import re
import tempfile
import threading
import unittest
from contextlib import closing
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.parse import urlsplit, parse_qs
from unittest.mock import patch
from app import google_auth
from urllib.request import build_opener, HTTPCookieProcessor, ProxyHandler, Request
from app.web_app import make_server
from app.service import TaxiService
from app.accounts import Accounts


class WebTests(unittest.TestCase):
    def setUp(self):
        self.google_config = {'client_id': 'test-client', 'client_secret': 'test-secret',
                              'redirect_uri': 'http://127.0.0.1:8000/auth/google/callback',
                              'admin_emails': ['owner@gmail.com']}
        self.config_patch = patch('app.google_auth.load_config', return_value=self.google_config)
        self.config_patch.start()
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / 'taxi.db'
        self.server = make_server(0, self.database)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.browser = self.new_browser()

    def new_browser(self):
        return build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))

    def tearDown(self):
        self.config_patch.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.directory.cleanup()

    def get(self, path, browser=None):
        with (browser or self.browser).open(self.url + path, timeout=10) as response:
            return response.read().decode()

    def csrf(self, path, browser=None):
        return re.search(r'name="csrf" value="([^"]+)"', self.get(path, browser)).group(1)

    def post(self, action, browser=None, **values):
        request = Request(self.url + action, urlencode(values).encode(), method='POST')
        with (browser or self.browser).open(request, timeout=10) as response:
            return response.read().decode()

    def register(self, role, phone, browser=None, invite=''):
        return self.post('/register/' + role, browser, csrf=self.csrf('/register/' + role, browser),
                         name='Test ' + role, phone=phone, password='TestPassword123', confirm='TestPassword123',
                         car='Test Car 001', invite=invite)

    def admin(self):
        self.get('/login/admin')
        with closing(TaxiService(self.database)) as service:
            accounts = Accounts(service)
            session = service.db.execute("SELECT * FROM sessions WHERE role='admin' ORDER BY expires DESC LIMIT 1").fetchone()
            url = google_auth.start(service.db, session['token_hash'], self.google_config)
            state = parse_qs(urlsplit(url).query)['state'][0]
        profile = {'sub': 'test-google-owner', 'email': 'owner@gmail.com', 'email_verified': True, 'name': 'Owner'}
        with patch('app.google_auth.exchange', return_value=profile):
            self.get('/auth/google/callback?' + urlencode({'state': state, 'code': 'test-code'}))

    def assert_http(self, code, callback):
        with self.assertRaises(HTTPError) as error:
            callback()
        self.assertEqual(error.exception.code, code)
        error.exception.close()

    def test_full_workflow_without_approval(self):
        self.admin()
        self.register('client', '+77000000002')
        result = self.register('driver', '+77000000003')
        self.assertIn('Вы готовы к новой поездке', result)
        self.post('/order', csrf=self.csrf('/client'), origin='Station', destination='School', distance=10, tariff='Комфорт')
        self.post('/discount', csrf=self.csrf('/admin'), order_id=1, percent=10)
        self.post('/assign', csrf=self.csrf('/admin'), order_id=1, driver_id=1)
        self.assertIn('Начать поездку', self.get('/driver'))
        self.post('/start', csrf=self.csrf('/driver'), order_id=1, driver_id=999)
        result = self.post('/complete', csrf=self.csrf('/driver'), order_id=1, waiting=5)
        self.assertIn('Итого: 1 800.00', result)
        self.assertIn('1 завершённых поездок', self.get('/admin'))

    def test_role_separation_and_logout(self):
        self.register('client', '+77000000002')
        self.assertIn('Управление сервисом', self.get('/admin'))
        self.assert_http(403, lambda: self.post('/assign', csrf=self.csrf('/client'), order_id=1, driver_id=1))
        old = self.csrf('/client')
        self.post('/logout/client', csrf=old)
        self.assertIn('Вход в кабинет клиента', self.get('/client'))
        self.assert_http(403, lambda: self.post('/order', csrf=old))
        self.post('/login/client', csrf=self.csrf('/login/client'), phone='+77000000002', password='TestPassword123')
        self.assertIn('Куда поедем сегодня?', self.get('/client'))

    def test_private_client_history(self):
        self.register('client', '+77000000002')
        self.post('/order', csrf=self.csrf('/client'), origin='Private origin', destination='Private destination', distance=5, tariff='Эконом')
        other = self.new_browser()
        self.register('client', '+77000000004', other)
        self.assertNotIn('Private origin', self.get('/client', other))
        self.assertIn('Private origin', self.get('/client'))

    def test_admin_invitation_and_driver_invitation(self):
        self.assert_http(403, lambda: self.register('admin', '+77000000001'))
        self.admin()
        result = self.post('/invite-driver', csrf=self.csrf('/admin'), name='New Driver', phone='+77000000003', car='New Car')
        code = re.search(r'<input readonly value="([^"]+)"', result).group(1)
        self.assertIn('Вы готовы к новой поездке', self.register('driver', '+77000000003', invite=code))
        other = self.new_browser()
        self.assert_http(400, lambda: self.register('driver', '+77000000004', other, code))

    def test_csrf_and_no_demo_drivers(self):
        self.assert_http(403, lambda: self.post('/register/client', csrf='bad'))
        self.admin()
        result = self.get('/admin')
        for text in ('Арман', 'Данияр', 'Алия', 'Учебный проект', 'Демо-режим'):
            self.assertNotIn(text, result)
        self.assertNotIn('nav-link', result)
        self.assertIn('Зарегистрированных водителей пока нет', result)

    def test_driver_cannot_operate_another_drivers_trip(self):
        self.admin()
        self.register('client', '+77000000002')
        self.register('driver', '+77000000003')
        other = self.new_browser()
        self.register('driver', '+77000000004', other)
        self.post('/order', csrf=self.csrf('/client'), origin='Only driver one', destination='B', distance=5, tariff='Эконом')
        self.post('/assign', csrf=self.csrf('/admin'), order_id=1, driver_id=1)
        self.assertNotIn('Only driver one', self.get('/driver?driver=1', other))
        self.assert_http(400, lambda: self.post('/start', other, csrf=self.csrf('/driver', other), order_id=1, driver_id=1))

    def test_public_page_has_only_two_roles(self):
        result = self.get('/')
        self.assertIn('/login/client', result)
        self.assertIn('/login/driver', result)
        self.assertNotIn('/login/admin', result)
        self.assertNotIn('Для администратора', result)
        result = self.get('/register/admin')
        self.assertIn('Войти через Google', result)
        self.assertNotIn('name="password"', result)

    def test_google_state_and_allowlist(self):
        self.get('/login/admin')
        with closing(TaxiService(self.database)) as service:
            accounts = Accounts(service)
            session = service.db.execute("SELECT * FROM sessions WHERE role='admin'").fetchone()
            url = google_auth.start(service.db, session['token_hash'], self.google_config)
            state = parse_qs(urlsplit(url).query)['state'][0]
            profile = {'sub': 'other', 'email': 'other@gmail.com', 'email_verified': True}
            with patch('app.google_auth.exchange', return_value=profile):
                with self.assertRaises(ValueError):
                    google_auth.finish(accounts, state, 'code', 'wrong-session', self.google_config)
                with self.assertRaises(ValueError):
                    google_auth.finish(accounts, state, 'code', session['token_hash'], self.google_config)
            with self.assertRaises(ValueError):
                google_auth.finish(accounts, state, 'code', session['token_hash'], self.google_config)
        self.assertFalse(google_auth.allowed({'sub': '1', 'email': 'owner@gmail.com', 'email_verified': False}, self.google_config))

    def test_google_missing_config_fails_closed(self):
        with patch('app.google_auth.load_config', return_value={}):
            result = self.get('/login/admin')
            self.assertIn('disabled', result)
            self.assertIn('пока не подключён', result)

    def google_public(self, role, subject='public-google'):
        self.get('/login/' + role)
        with closing(TaxiService(self.database)) as service:
            Accounts(service)
            session = service.db.execute('SELECT * FROM sessions WHERE role=? ORDER BY expires DESC LIMIT 1', (role,)).fetchone()
            url = google_auth.start(service.db, session['token_hash'], self.google_config, role)
            state = parse_qs(urlsplit(url).query)['state'][0]
        profile = {'sub': subject, 'email': 'public@gmail.com', 'email_verified': True, 'name': 'Public User'}
        with patch('app.google_auth.exchange', return_value=profile):
            return self.get('/auth/google/callback?' + urlencode({'state': state, 'code': 'public-code'}))

    def test_google_client_registration_and_repeat_login(self):
        self.assertIn('Продолжить с Google', self.get('/register/client'))
        self.assertIn('Куда поедем сегодня?', self.google_public('client'))
        self.post('/order', csrf=self.csrf('/client'), origin='Google Trip', destination='B', distance=5, tariff='Эконом')
        self.post('/logout/client', csrf=self.csrf('/client'))
        self.assertIn('Google Trip', self.google_public('client'))
        self.assertIn('Управление сервисом', self.get('/admin'))
        with closing(TaxiService(self.database)) as service:
            self.assertEqual(service.db.execute("SELECT COUNT(*) FROM users WHERE role='client'").fetchone()[0], 1)

    def test_google_driver_profile_activates_immediately(self):
        self.assertIn('Завершите регистрацию', self.google_public('driver'))
        self.assert_http(403, lambda: self.post('/start', csrf=self.csrf('/driver'), order_id=1))
        result = self.post('/driver-profile', csrf=self.csrf('/driver'), phone='+77000000007', car='Toyota 007')
        self.assertIn('Вы готовы к новой поездке', result)
        self.assert_http(400, lambda: self.post('/driver-profile', csrf=self.csrf('/driver'), phone='+77000000007', car='Changed'))
        self.admin()
        self.assertIn('Toyota 007', self.get('/admin'))
        self.assertIn('Вы готовы к новой поездке', self.get('/driver'))
        self.post('/logout/driver', csrf=self.csrf('/driver'))
        self.assert_http(400, lambda: self.post('/login/driver', csrf=self.csrf('/login/driver'), phone='+77000000007', password='TestPassword123'))
        self.assertIn('Вы готовы к новой поездке', self.google_public('driver'))

    def test_google_same_identity_keeps_roles_separate(self):
        self.google_public('client')
        self.google_public('driver')
        with closing(TaxiService(self.database)) as service:
            self.assertEqual(service.db.execute('SELECT COUNT(*) FROM google_accounts').fetchone()[0], 2)
            self.assertEqual(service.db.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
