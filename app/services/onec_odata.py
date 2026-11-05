"""Ограниченный обмен с опубликованным OData 1С: товары/закупки → сайт → цены.

Запись разрешена только в виды цен и документы установки цен. Ни поступления,
ни количества, ни банковские документы этот клиент не изменяет.
"""
import json
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import quote, urlencode, urlsplit
from zoneinfo import ZoneInfo

import httpx

ZERO = '00000000-0000-0000-0000-000000000000'
PRICE_DOC = 'Document_УстановкаЦенНоменклатуры'
PRICE_TYPES = 'Catalog_ВидыЦен'
TIERS = {'Розница': 'retail', 'ОПТ': 'wholesale', 'СуперОПТ': 'super_wholesale'}


def onec_now():
    return datetime.now(ZoneInfo('Europe/Moscow')).replace(tzinfo=None, microsecond=0).isoformat()


def query_url(base, resource, params=None):
    # The deployed 1C misinterprets '+' in filters: `eq false` selected folders.
    # Encode spaces as %20, as required by the OData URI grammar.
    return base.rstrip('/') + '/' + resource + '?' + urlencode(
        {'$format': 'json', **(params or {})}, quote_via=quote, safe='$,()')


class OneCClient:
    def __init__(self, base, login, password, *, transport=None):
        parsed = urlsplit(base)
        if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError('OData requires an HTTPS URL without embedded credentials')
        if not login or not password:
            raise ValueError('OData service credentials are missing')
        self.base = base
        self.client = httpx.Client(timeout=40, trust_env=False, follow_redirects=False,
                                   auth=(login, password), transport=transport)

    def close(self):
        self.client.close()

    def get(self, resource, params=None):
        for attempt in range(3):
            try:
                response = self.client.get(query_url(self.base, resource, params))
                if response.status_code >= 500 and attempt < 2:
                    time.sleep(attempt + 1)
                    continue
                response.raise_for_status()
                return response.json()
            except httpx.TransportError:
                if attempt == 2:
                    raise
                time.sleep(attempt + 1)

    def rows(self, resource, params=None):
        result, skip = [], 0
        while True:
            page = self.get(resource, {**(params or {}), '$top': 1000, '$skip': skip}).get('value', [])
            result.extend(page)
            if len(page) < 1000:
                return result
            skip += len(page)

    def write(self, resource, payload=None, *, method='POST'):
        if resource not in (PRICE_TYPES, PRICE_DOC) and not resource.startswith(PRICE_DOC + '('):
            raise ValueError('Writing this 1C resource is not allowed')
        # Never blindly retry writes: the server may have committed before a timeout.
        response = self.client.request(method, query_url(self.base, resource), json=payload)
        if response.is_error:
            raise RuntimeError(f'1C {method} {resource}: {response.status_code}: {response.text[:1800]}')
        return response.json() if response.content else None

    def snapshot(self):
        catalog = self.rows('Catalog_Номенклатура', {
            '$filter': 'IsFolder eq false', '$orderby': 'Ref_Key',
            '$select': 'Ref_Key,Code,Description,DeletionMark,Артикул,ЕдиницаИзмерения_Key'})
        if not catalog:
            raise ValueError('1C returned an empty catalogue')
        currencies = self.rows('Catalog_Валюты', {'$select': 'Ref_Key,Code,Description'})
        rub = [c['Ref_Key'] for c in currencies if c.get('Code') == '643']
        if len(rub) != 1:
            raise ValueError('Cannot uniquely identify RUB in 1C')
        receipts = self.rows('Document_ПриобретениеТоваровУслуг', {
            '$filter': 'Posted eq true and DeletionMark eq false', '$orderby': 'Date desc,Ref_Key desc',
            '$select': 'Ref_Key,Number,Date,Валюта_Key,ЦенаВключаетНДС,Товары'})
        from app.config import settings
        cost_types = [t for t in self.price_types() if t['Description'] == settings.ONEC_PRICE_TYPE_PURCHASE]
        if len(cost_types) > 1:
            raise ValueError('Several purchase price types have the same name')
        cost_prices = [p for p in self.current_prices().values()
                       if cost_types and p['ВидЦены_Key'] == cost_types[0]['Ref_Key']] if cost_types else []
        result = dict(catalog=catalog, rub=rub[0], receipts=receipts,
                    purchase_types=cost_types, purchase_prices=cost_prices,
                    barcodes=self.rows('InformationRegister_ШтрихкодыНоменклатуры'))
        if settings.ONEC_IMPORT_STOCKS:
            result['stock'] = self.stock_snapshot()
        return result

    def stock_snapshot(self):
        warehouses = self.rows('Catalog_Склады', {'$filter':'IsFolder eq false and DeletionMark eq false',
                                                 '$select':'Ref_Key,Description,ТипСклада'})
        stamp = onec_now()
        balances = self.rows(f"AccumulationRegister_ТоварыНаСкладах/Balance(Period=datetime'{stamp}',Condition='',Dimensions='Номенклатура,Характеристика,Склад')")
        return {'warehouses':warehouses, 'balances':balances, 'fetched_at':stamp, 'complete':True}

    def price_types(self):
        return self.rows(PRICE_TYPES, {'$filter': 'IsFolder eq false and DeletionMark eq false'})

    def ensure_types(self, rub):
        found = defaultdict(list)
        for row in self.price_types():
            found[row['Description']].append(row)
        result = {}
        for name, tier in TIERS.items():
            if len(found[name]) > 1:
                raise ValueError(f'Существует несколько видов цен «{name}»')
            if found[name]:
                row = found[name][0]
                if row.get('ВалютаЦены_Key') != rub or row.get('СпособЗаданияЦены') != 'Вручную':
                    raise ValueError(f'Вид цены «{name}» требует проверки валюты/формулы')
            else:
                row = self.write(PRICE_TYPES, {
                    'Description': name, 'IsFolder': False, 'ВалютаЦены_Key': rub,
                    'СпособЗаданияЦены': 'Вручную', 'Статус': 'Действует',
                    'Идентификатор': 'PARTS_' + tier, 'ИспользоватьПриПродаже': True,
                    'ИспользоватьПриОптовойПродаже': tier != 'retail',
                    'ИспользоватьПриРозничнойПродаже': tier == 'retail',
                    'ЦенаВключаетНДС': False, 'ТочностьОкругления': 1, 'Округлять': True})
            result[tier] = row['Ref_Key']
        return result

    def current_prices(self):
        result = {}
        # Recorder-subordinate registers expose methods on *_RecordType, as
        # specified by this database's metadata bindingParameter.
        for register in ['ЦеныНоменклатуры', 'ЦеныНоменклатуры25']:
            path = f"InformationRegister_{register}_RecordType/SliceLast(Period=datetime'{onec_now()}',Condition='')"
            for row in self.rows(path):
                if row.get('Active') is False or any(row.get(k) not in (None, '', ZERO) for k in
                    ['Характеристика_Key', 'ХарактеристикаЦО_Key', 'СерияЦО_Key', 'УпаковкаЦО_Key', 'Упаковка_Key']):
                    continue
                result[(row['Номенклатура_Key'], row['ВидЦены_Key'])] = row
        return result


def purchase_costs(receipts, rub):
    """Последняя проведённая закупка, стоимость строки на базовую единицу.

    Повторяющиеся строки одного товара в одном документе усредняются по количеству.
    Это закупочная цена для наценки; партийную себестоимость FIFO рассчитывает 1С.
    """
    result, skipped = {}, []
    for document in sorted(receipts, key=lambda d: (d['Date'], d['Ref_Key']), reverse=True):
        if document['Date'] > onec_now() or document.get('Валюта_Key') != rub:
            skipped.append(document['Ref_Key'])
            continue
        grouped = defaultdict(lambda: [Decimal(0), Decimal(0)])
        for row in document.get('Товары', []):
            if row.get('Характеристика_Key') not in (None, '', ZERO):
                continue
            quantity = Decimal(str(row.get('Количество', 0)))
            amount = Decimal(str(row.get('СуммаСНДС', row.get('Сумма', 0))))
            if not quantity.is_finite() or not amount.is_finite() or quantity <= 0 or amount <= 0:
                continue
            key = row['Номенклатура_Key']
            grouped[key][0] += quantity
            grouped[key][1] += amount
        for key, (quantity, amount) in grouped.items():
            result.setdefault(key, dict(cost=(amount / quantity).quantize(Decimal('.01'), rounding=ROUND_HALF_UP),
                                        document=document['Ref_Key'], date=document['Date']))
    return result, skipped


def commerce_xml(snapshot, name_map):
    """Reuse the tested CommerceML importer and its GUID/SKU conflict checks."""
    barcodes = defaultdict(set)
    for b in snapshot['barcodes']:
        if all(b.get(k) in (None, '', ZERO) for k in ['Характеристика_Key', 'Серия_Key', 'Упаковка_Key']):
            barcodes[b['Номенклатура_Key']].add(b['Штрихкод'])
    root = ET.Element('КоммерческаяИнформация')
    goods = ET.SubElement(ET.SubElement(root, 'Каталог'), 'Товары')
    for row in snapshot['catalog']:
        key, name = row['Ref_Key'], row['Description']
        codes = barcodes[key]
        mapped = name_map.get(name.casefold())
        barcode = mapped if mapped in codes else next(iter(codes)) if len(codes) == 1 else ''
        from app.config import settings
        sku = ((row.get('Артикул') or '').strip() or barcode or (row.get('Code') if settings.ONEC_SOURCE_ONLY else mapped) or key)
        item = ET.SubElement(goods, 'Товар', {'Статус': 'Удален'} if row.get('DeletionMark') else {})
        for tag, value in [('Ид', key), ('Наименование', name), ('Артикул', sku), ('Штрихкод', barcode)]:
            ET.SubElement(item, tag).text = value
    return ET.tostring(root, encoding='utf-8')


def add_purchase_price_overrides(costs, prices, rub):
    """Explicit purchase tariff can update a cost after the latest receipt."""
    result = dict(costs)
    for row in prices:
        key = row['Номенклатура_Key']
        value = Decimal(str(row.get('Цена', 0)))
        if row.get('Валюта_Key') != rub or not value.is_finite() or value <= 0:
            continue
        if key not in result or row['Period'] > result[key]['date']:
            result[key] = dict(cost=value.quantize(Decimal('.01'), rounding=ROUND_HALF_UP),
                               document=row.get('Recorder'), date=row['Period'])
    return result


def changed_price_rows(items, type_ids, current, rub):
    result = []
    for item in items:
        if not item.get('id'):
            continue
        for tier, type_id in type_ids.items():
            value = item.get(tier)
            if value is None:
                continue
            number = Decimal(str(value))
            if not number.is_finite() or number <= 0 or number != number.to_integral_value():
                raise ValueError('Selling prices must be positive whole rubles')
            old = current.get((item['id'], type_id))
            if old and Decimal(str(old.get('Цена', 0))) == number and old.get('Валюта_Key') == rub:
                continue
            result.append(dict(Номенклатура_Key=item['id'], ВидЦены_Key=type_id,
                               Цена=int(number), Валюта_Key=rub,
                               Упаковка_Key=ZERO, ЦенаИзмененаВручную=True))
    return sorted(result, key=lambda r: (r['Номенклатура_Key'], r['ВидЦены_Key']))


def price_document(rows, *, comment, price_table='Товары2_5'):
    if price_table not in ('Товары', 'Товары2_5'):
        raise ValueError('Unknown price document table')
    if not rows:
        raise ValueError('Cannot create an empty price document')
    lines = []
    for number, row in enumerate(rows, 1):
        characteristic_fields = ({'ХарактеристикаЦО_Key': ZERO, 'СерияЦО_Key': ZERO, 'УпаковкаЦО_Key': ZERO}
                                 if price_table == 'Товары2_5' else {'Характеристика_Key': ZERO})
        lines.append({**row, **characteristic_fields, 'LineNumber': str(number)})
    return {'Date': onec_now(), 'Комментарий': comment,
            price_table: lines,
            'ВидыЦен': [{'LineNumber': str(i), 'ВидЦены_Key': k}
                        for i, k in enumerate(sorted({r['ВидЦены_Key'] for r in rows}), 1)]}
