import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from app.service import TaxiCalculator, TaxiService


class CalculatorTests(unittest.TestCase):
    def setUp(self):
        self.calc = TaxiCalculator()

    def test_three_call_signatures(self):
        self.assertEqual(self.calc.taxiCost(10), Decimal('1200.00'))
        self.assertEqual(self.calc.taxiCost(10, 180), Decimal('1800.00'))
        self.assertEqual(self.calc.taxiCost(10, 180, 5), Decimal('2000.00'))

    def test_discount_and_decimal_input(self):
        quote = self.calc.quote('2,5', 120, 3, 10)
        self.assertEqual(quote, {'ride': Decimal('300'), 'waiting': Decimal('120'),
                                'discount': Decimal('42'), 'total': Decimal('378')})

    def test_invalid_values(self):
        for value in ('', 'nan', 'inf', '-1', '0', '3001', 'abc'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.calc.taxiCost(value)
        for value in (-1, 1.5, 1441, 'NaN'):
            with self.subTest(waiting=value), self.assertRaises(ValueError):
                self.calc.taxiCost(5, 120, value)
        for value in (-1, 51, 'inf'):
            with self.subTest(discount=value), self.assertRaises(ValueError):
                self.calc.quote(5, discount=value)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.s = TaxiService(':memory:')
        with self.s.db:
            self.s.db.executemany("INSERT INTO drivers(id,name,car,approval) VALUES (?,?,?,'approved')",
                                  [(1, 'Test One', 'Car One'), (2, 'Test Two', 'Car Two')])

    def tearDown(self):
        self.s.close()

    def create(self, distance=10):
        return self.s.create_order('Анна', 'Вокзал', 'Университет', distance, 'Комфорт')

    def finish(self, oid, driver=1, waiting=0):
        self.s.assign_driver(oid, driver)
        self.s.start_trip(oid, driver)
        self.s.complete_trip(oid, driver, waiting)

    def test_full_lifecycle(self):
        oid = self.create()
        self.s.set_discount(oid, 10)
        self.finish(oid, waiting=5)
        self.assertEqual(self.s.order(oid)['total_cents'], 180000)
        self.assertEqual(self.s.order(oid)['status'], 'Завершён')
        self.assertFalse(self.s.drivers()[0]['busy'])
        self.assertEqual(self.s.statistics()['income'], Decimal('1800'))
        self.assertEqual(self.s.statistics()['count'], 1)

    def test_busy_driver_and_state_rules(self):
        first, second = self.create(), self.create()
        self.s.assign_driver(first, 1)
        for action in [lambda: self.s.assign_driver(second, 1),
                       lambda: self.s.assign_driver(first, 2),
                       lambda: self.s.start_trip(first, 2),
                       lambda: self.s.complete_trip(first, 1, 0)]:
            with self.assertRaises(ValueError):
                action()
        self.s.start_trip(first, 1)
        with self.assertRaises(ValueError):
            self.s.complete_trip(first, 2, 0)
        self.s.complete_trip(first, 1, 0)
        with self.assertRaises(ValueError):
            self.s.complete_trip(first, 1, 0)
        with self.assertRaises(ValueError):
            self.s.set_discount(first, 10)
        self.s.assign_driver(second, 1)

    def test_daily_income_and_longest(self):
        old, today, pending = self.create(30), self.create(5), self.create(100)
        self.finish(old)
        with self.s.db:
            self.s.db.execute("UPDATE orders SET completed_at='2001-01-01T12:00:00' WHERE id=?", (old,))
        self.finish(today)
        stats = self.s.statistics()
        self.assertEqual(stats['income'], Decimal('900'))
        self.assertEqual(stats['count'], 1)
        self.assertEqual(stats['active'], 1)
        self.assertEqual(stats['longest']['id'], old)
        self.assertEqual(self.s.statistics('2001-01-01')['income'], Decimal('5400'))

    def test_empty_statistics_and_validation(self):
        self.assertIsNone(self.s.statistics()['longest'])
        self.assertEqual(self.s.statistics()['income'], 0)
        for args in [('', 'A', 'B', 2, 'Эконом'), ('Аня', 'A', 'a', 2, 'Эконом'),
                     ('Аня', 'A', 'B', 2, 'Несуществующий')]:
            with self.assertRaises(ValueError):
                self.s.create_order(*args)

    def test_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'taxi.db'
            first = TaxiService(path)
            oid = first.create_order('Анна', 'A', 'B', 5, 'Эконом')
            first.close()
            second = TaxiService(path)
            self.assertEqual(second.order(oid)['client'], 'Анна')
            self.assertEqual(len(second.drivers()), 0)
            second.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
