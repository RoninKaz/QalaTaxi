"""Бизнес-логика такси: расчёты, SQLite и жизненный цикл заказов."""
import os
import sqlite3
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import overload

TARIFFS = {'Эконом': 120, 'Комфорт': 180, 'Бизнес': 260}
WAITING_RATE = 40
STATUSES = ('Новый', 'Назначен', 'В пути', 'Завершён')


def number(value, title, minimum=0, maximum=100000):
    try:
        result = Decimal(str(value).replace(',', '.'))
    except (InvalidOperation, ValueError):
        raise ValueError(f'{title}: введите число.') from None
    if not result.is_finite() or not minimum <= result <= maximum:
        raise ValueError(f'{title}: допустимо от {minimum} до {maximum}.')
    return result


def money(value):
    return Decimal(value).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


class TaxiCalculator:
    """@overload описывает сигнатуры; исполнение — параметры по умолчанию."""
    @overload
    def taxiCost(self, distance: float) -> Decimal: ...

    @overload
    def taxiCost(self, distance: float, tariff: float) -> Decimal: ...

    @overload
    def taxiCost(self, distance: float, tariff: float, waitingMinutes: int) -> Decimal: ...

    def taxiCost(self, distance, tariff=120, waitingMinutes=0):
        distance = number(distance, 'Расстояние, км', Decimal('0.1'), 3000)
        tariff = number(tariff, 'Тариф', 1, 10000)
        return money(distance * tariff + self.waitingCost(waitingMinutes))

    def waitingCost(self, minutes):
        minutes = number(minutes, 'Ожидание, мин', 0, 1440)
        if minutes != minutes.to_integral_value():
            raise ValueError('Ожидание задаётся целым числом минут.')
        return money(minutes * WAITING_RATE)

    def quote(self, distance, tariff=120, waiting=0, discount=0):
        gross = self.taxiCost(distance, tariff, waiting)
        percent = number(discount, 'Скидка, %', 0, 50)
        saving = money(gross * percent / 100)
        wait = self.waitingCost(waiting)
        return {'ride': gross - wait, 'waiting': wait, 'discount': saving,
                'total': gross - saving}


def default_database():
    """DATABASE_PATH из окружения, иначе taxi.db в корне проекта."""
    path = Path(os.environ.get('DATABASE_PATH') or Path(__file__).resolve().parent.parent / 'taxi.db')
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


class TaxiService:
    def __init__(self, database=None):
        path = database if database is not None else default_database()
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys = ON')
        self.calculator = TaxiCalculator()
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS drivers (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, car TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client TEXT NOT NULL, origin TEXT NOT NULL, destination TEXT NOT NULL,
                distance REAL NOT NULL, tariff TEXT NOT NULL, rate INTEGER NOT NULL,
                waiting INTEGER NOT NULL DEFAULT 0, discount REAL NOT NULL DEFAULT 0,
                total_cents INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'Новый',
                driver_id INTEGER REFERENCES drivers(id),
                created_at TEXT NOT NULL, completed_at TEXT);
        ''')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            for table, column, definition in [
                ('drivers', 'user_id', 'INTEGER'),
                ('drivers', 'approval', "TEXT NOT NULL DEFAULT 'pending'"),
                ('orders', 'client_id', 'INTEGER'),
                ('orders', 'driver_label', 'TEXT')]:
                columns = {r['name'] for r in self.db.execute(f'PRAGMA table_info({table})')}
                if column not in columns:
                    self.db.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')
            self.db.execute("UPDATE drivers SET approval='approved' WHERE approval IN ('pending','rejected') AND user_id IS NOT NULL AND trim(car) != ''")
            for name, car in [('Арман С.', 'Hyundai Accent · 101 AAA'),
                              ('Данияр К.', 'Toyota Camry · 202 BBB'),
                              ('Алия М.', 'Kia K5 · 303 CCC')]:
                old = self.db.execute('SELECT id FROM drivers WHERE name=? AND car=? AND user_id IS NULL', (name, car)).fetchone()
                if old:
                    self.db.execute("UPDATE orders SET driver_label=?, driver_id=NULL WHERE driver_id=? AND status='Завершён'", (name, old['id']))
                    self.db.execute("UPDATE orders SET driver_id=NULL, status='Новый' WHERE driver_id=?", (old['id'],))
                    self.db.execute('DELETE FROM drivers WHERE id=?', (old['id'],))

    @staticmethod
    def now():
        return datetime.now().isoformat(timespec='seconds')

    def create_order(self, client, origin, destination, distance, tariff, client_id=None):
        fields = [str(v).strip() for v in (client, origin, destination)]
        if any(not v or len(v) > 120 for v in fields):
            raise ValueError('Имя и оба адреса обязательны (до 120 символов).')
        if fields[1].casefold() == fields[2].casefold():
            raise ValueError('Укажите разные адреса отправления и назначения.')
        if tariff not in TARIFFS:
            raise ValueError('Выберите тариф из списка.')
        total = self.calculator.taxiCost(distance, TARIFFS[tariff])
        km = float(number(distance, 'Расстояние', Decimal('0.1'), 3000))
        with self.db:
            cursor = self.db.execute('''INSERT INTO orders
                (client, origin, destination, distance, tariff, rate, total_cents, created_at, client_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (*fields, km, tariff, TARIFFS[tariff], int(total * 100), self.now(), client_id))
        return cursor.lastrowid

    def orders(self, driver_id=None, client_id=None):
        sql = '''SELECT o.*, COALESCE(d.name, o.driver_label) AS driver_name FROM orders o
                 LEFT JOIN drivers d ON d.id = o.driver_id'''
        args = ()
        if driver_id is not None:
            sql += ' WHERE o.driver_id = ?'
            args = (driver_id,)
        elif client_id is not None:
            sql += ' WHERE o.client_id = ?'
            args = (client_id,)
        return self.db.execute(sql + ' ORDER BY o.id DESC', args).fetchall()

    def order(self, order_id):
        row = self.db.execute('SELECT * FROM orders WHERE id = ?', (order_id,)).fetchone()
        if row is None:
            raise ValueError('Заказ не найден.')
        return row

    def drivers(self):
        return self.db.execute('''SELECT d.*, EXISTS(SELECT 1 FROM orders o
            WHERE o.driver_id = d.id AND o.status IN ('Назначен', 'В пути')) AS busy
            FROM drivers d ORDER BY d.id''').fetchall()

    def assign_driver(self, order_id, driver_id):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            order = self.order(order_id)
            if order['status'] != 'Новый':
                raise ValueError('Назначить водителя можно только новому заказу.')
            driver = next((d for d in self.drivers() if d['id'] == driver_id), None)
            if driver is None or driver['busy'] or driver['approval'] != 'approved':
                raise ValueError('Водитель недоступен. Выберите свободного водителя.')
            self.db.execute("UPDATE orders SET driver_id=?, status='Назначен' WHERE id=?",
                            (driver_id, order_id))

    def start_trip(self, order_id, driver_id):
        with self.db:
            cursor = self.db.execute("""UPDATE orders SET status='В пути'
                WHERE id=? AND driver_id=? AND status='Назначен'""", (order_id, driver_id))
            if cursor.rowcount != 1:
                raise ValueError('Можно начать только назначенный вам заказ.')

    def set_discount(self, order_id, percent):
        percent = number(percent, 'Скидка, %', 0, 50)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            order = self.order(order_id)
            if order['status'] == 'Завершён':
                raise ValueError('Завершённый заказ нельзя изменять.')
            quote = self.calculator.quote(order['distance'], order['rate'], order['waiting'], percent)
            self.db.execute('UPDATE orders SET discount=?, total_cents=? WHERE id=?',
                            (float(percent), int(quote['total'] * 100), order_id))

    def complete_trip(self, order_id, driver_id, waiting):
        self.calculator.waitingCost(waiting)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            order = self.order(order_id)
            if order['status'] != 'В пути' or order['driver_id'] != driver_id:
                raise ValueError('Можно завершить только свой начатый заказ.')
            quote = self.calculator.quote(order['distance'], order['rate'], waiting, order['discount'])
            self.db.execute("""UPDATE orders SET waiting=?, total_cents=?,
                status='Завершён', completed_at=? WHERE id=?""",
                (int(Decimal(str(waiting).replace(',', '.'))), int(quote['total'] * 100), self.now(), order_id))
        return quote

    def statistics(self, day=None):
        day = day or datetime.now().date().isoformat()
        row = self.db.execute('''SELECT COUNT(*) AS count, COALESCE(SUM(total_cents), 0) AS income
            FROM orders WHERE status='Завершён' AND substr(completed_at, 1, 10)=?''', (day,)).fetchone()
        longest = self.db.execute("""SELECT * FROM orders WHERE status='Завершён'
            ORDER BY distance DESC, id DESC LIMIT 1""").fetchone()
        active = self.db.execute("SELECT COUNT(*) FROM orders WHERE status != 'Завершён'").fetchone()[0]
        return {'count': row['count'], 'income': Decimal(row['income']) / 100,
                'active': active, 'longest': longest}

    def close(self):
        self.db.close()
