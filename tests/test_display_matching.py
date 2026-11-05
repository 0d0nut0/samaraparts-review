import unittest
from app.services.price_matching import PriceMatcher


class DisplayMatchingTest(unittest.TestCase):
    def test_supplied_alias_matches_updated_description(self):
        row = {'article': 'LCD-1', 'name': 'Дисплей для Infinix Hot 30i (X669D) модуль с рамкой Черный - OR (HD+, 300 Кд/м², 90 Гц)', 'price': 1100}
        matcher = PriceMatcher([row])
        self.assertEqual(matcher.match({'moba_name': 'Дисплей для Infinix Hot 30i модуль Черный - OR'}).product, row)
        for wrong in ['Дисплей для Infinix Hot 30i + тач Черный - OR',
                      'Дисплей для Infinix Hot 30 модуль Черный - OR',
                      'Дисплей для Infinix Hot 30i модуль Белый - OR',
                      'Дисплей для Infinix Hot 30i модуль Черный - OR100',
                      'Дисплей для Infinix Hot 30i (X666D) модуль Черный - OR']:
            self.assertIsNone(matcher.match({'moba_name': wrong}).product, wrong)

    def test_ambiguous_panel_and_matrix_quality_are_not_guessed(self):
        rows = [{'article': str(i), 'name': f'Дисплей для iPhone 13 (A2635) в сборе с тачскрином Черный - (Soft OLED) (Full HD, 500 Кд/м², {hz} Гц)'}
                for i, hz in enumerate([60, 120])]
        matcher = PriceMatcher(rows)
        self.assertEqual(matcher.match({'moba_name': 'Дисплей для iPhone 13 + тач Черный - (Soft OLED)'}).reason, 'ambiguous')
        self.assertIsNone(matcher.match({'moba_name': 'Дисплей для iPhone 13 + тач Черный - (Hard OLED)'}).product)
        self.assertIsNone(matcher.match({'name': 'Дисплей для iPhone 13 + тач Черный - (Soft OLED)'}).product)
