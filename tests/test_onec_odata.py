import unittest
from decimal import Decimal
from unittest.mock import patch

import httpx

from app.services.onec_odata import (OneCClient, ZERO, query_url, purchase_costs,
    add_purchase_price_overrides, changed_price_rows, commerce_xml, price_document)


class OneCODataTest(unittest.TestCase):
    def test_filter_spaces_and_pagination_preserve_server_semantics(self):
        requests = []
        def response(request):
            requests.append(request)
            # The production 1C treats '+' differently from URI-encoded spaces.
            self.assertNotIn('+', str(request.url))
            self.assertIn('IsFolder%20eq%20false', str(request.url))
            skip = int(request.url.params['$skip'])
            return httpx.Response(200, json={'value': [{'id': i} for i in range(1000)] if skip == 0 else [{'id': 1000}]})
        client = OneCClient('https://onec.example/odata/standard.odata/', 'api', 'secret', transport=httpx.MockTransport(response))
        try:
            rows = client.rows('Catalog_Номенклатура', {'$filter': 'IsFolder eq false'})
            self.assertEqual(len(rows), 1001)
            self.assertEqual(len(requests), 2)
            with self.assertRaises(ValueError):
                client.write('Document_ПриобретениеТоваровУслуг', {})
        finally:
            client.close()

    def test_latest_receipt_uses_weighted_paid_unit_price_and_skips_other_currency(self):
        def doc(date, price, qty=2, currency='rub'):
            return {'Date': date, 'Ref_Key': date, 'Валюта_Key': currency,
                    'Товары': [{'Номенклатура_Key': 'p', 'Количество': qty, 'СуммаСНДС': price * qty, 'Характеристика_Key': ZERO}]}
        newer = doc('2020-02-02T00:00:00', 10)
        newer['Товары'].append({'Номенклатура_Key': 'p', 'Количество': 1, 'СуммаСНДС': 13})
        costs, skipped = purchase_costs([doc('2020-01-01T00:00:00', 1), newer,
                                         doc('2020-03-03T00:00:00', 999, currency='usd')], 'rub')
        self.assertEqual(costs['p']['cost'], Decimal('11.00'))
        self.assertEqual(len(skipped), 1)
        revised = add_purchase_price_overrides(costs, [{'Номенклатура_Key': 'p', 'Цена': 12.34,
                  'Валюта_Key': 'rub', 'Period': '2020-04-04T00:00:00'}], 'rub')
        self.assertEqual(revised['p']['cost'], Decimal('12.34'))

    def test_catalogue_map_wins_over_multiple_variant_barcodes(self):
        source = {'catalog': [{'Ref_Key': 'guid', 'Description': 'Short product', 'Артикул': ''}],
                  'barcodes': [{'Номенклатура_Key': 'guid', 'Штрихкод': 'canonical'},
                               {'Номенклатура_Key': 'guid', 'Штрихкод': 'variant', 'Характеристика_Key': 'variant-id'}]}
        xml = commerce_xml(source, {'short product': 'canonical'}).decode()
        self.assertIn('<Артикул>canonical</Артикул>', xml)
        self.assertIn('<Ид>guid</Ид>', xml)
        self.assertNotIn('variant', xml)

    def test_only_changed_known_whole_prices_are_written(self):
        items = [{'id': 'p', 'retail': 1200, 'wholesale': 1000, 'super_wholesale': None}]
        current = {('p', 'retail-type'): {'Цена': 1200, 'Валюта_Key': 'rub'}}
        rows = changed_price_rows(items, {'retail': 'retail-type', 'wholesale': 'opt-type', 'super_wholesale': 'super-type'}, current, 'rub')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['Цена'], 1000)
        data = price_document(rows, comment='PARTS sync hash')
        self.assertEqual(data['Товары2_5'][0]['Номенклатура_Key'], 'p')
        with self.assertRaises(ValueError):
            changed_price_rows([{'id': 'p', 'retail': 1.1}], {'retail': 'r'}, {}, 'rub')

    def test_uncertain_write_is_not_automatically_retried(self):
        attempts = []
        def lost(request):
            attempts.append(request)
            raise httpx.ReadTimeout('response lost after write', request=request)
        client = OneCClient('https://onec.example/odata/standard.odata/', 'api', 'secret', transport=httpx.MockTransport(lost))
        try:
            with self.assertRaises(httpx.ReadTimeout):
                client.write('Document_УстановкаЦенНоменклатуры', {'Комментарий': 'owned'})
            self.assertEqual(len(attempts), 1)
        finally:
            client.close()
