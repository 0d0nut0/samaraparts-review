"""Сверка своих товаров с публичным снимком Moba, расчёт и применение цен.

По умолчанию записывает только JSON-план. --apply сохраняет цены на сайт.
--stock-city samara отдельно включает сверку количеств указанного склада;
количества устанавливаются, а не прибавляются. Другие склады не изменяются.
"""
import argparse
import csv
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urljoin

import openpyxl
from sqlalchemy import select

from app.database import SessionLocal
from app.models import Category, City, InventoryTransaction, MobaPrice, Product, Stock, SystemConfig
from app.models.inventory_transaction import TransactionType
from app.services.price_matching import PriceMatcher, PricingPeer, estimate_price, normalize_name, product_group
from app.services.pricing import reference_prices, round_money


def identifier(value):
    if value is None:
        return ''
    if isinstance(value, (int, float)) and int(value) == value:
        return str(int(value))
    return str(value).strip()


def read_catalog(path: Path):
    book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        rows, started, result, seen = book.active.iter_rows(values_only=True), False, [], set()
        for number, values in enumerate(rows, 1):
            values = list(values) + [None] * 11
            name = str(values[4] or '').strip()
            if name.casefold() == 'наименование рабочее партс':
                started = True
                continue
            if not started or not name or name.casefold() == 'итого':
                continue
            barcode = identifier(values[1])
            sku = barcode or 'PARTS-' + hashlib.sha256(name.casefold().encode()).hexdigest()[:16].upper()
            if sku in seen:
                raise ValueError(f'Повторяющийся идентификатор в строке {number}: {sku}')
            seen.add(sku)
            result.append({'source_row': number, 'sku': sku, 'barcode': barcode, 'name': name,
                           'moba_name': str(values[7] or '').strip(),
                           'print_name': str(values[8] or '').strip(),
                           'model': str(values[9] or '').strip(), 'quality': str(values[10] or '').strip()})
        if not result:
            raise ValueError('Не найдены строки исходного каталога')
        return result
    finally:
        book.close()


def read_stocks(path: Path):
    book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    result = {}
    try:
        for row in book.active.iter_rows(min_row=2, values_only=True):
            if not row[1]:
                continue
            barcode, qty = identifier(row[0]), Decimal(str(row[4]))
            if not barcode or not qty.is_finite() or qty < 0 or qty != int(qty):
                raise ValueError(f'Некорректный остаток {barcode}')
            result[barcode] = result.get(barcode, 0) + int(qty)
    finally:
        book.close()
    return result


def read_costs(path: Path):
    """Только явно переданная закупочная таблица; цену лояльности не считаем себестоимостью."""
    if path.suffix.lower() == '.csv':
        sample = path.read_text(encoding='utf-8-sig')
        dialect = csv.Sniffer().sniff(sample[:8192], delimiters=';,\t,')
        rows = list(csv.reader(sample.splitlines(), dialect))
    else:
        book = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            rows = list(book.active.values)
        finally:
            book.close()
    columns = None
    values = {}
    for row in rows:
        names = {str(v or '').strip().casefold(): i for i, v in enumerate(row)}
        cost_index = next((names[k] for k in ('себестоимость', 'закупочная цена', 'закупочная стоимость',
                                              'purchase_price', 'cost', 'цена') if k in names), None)
        key_index = next((names[k] for k in ('штрихкод', 'barcode', 'sku', 'артикул') if k in names), None)
        name_index = next((names[k] for k in ('наименование', 'номенклатура', 'товары (работы, услуги)',
                                              'name') if k in names), None)
        if columns is None and cost_index is not None and (key_index is not None or name_index is not None):
            columns = (cost_index, key_index, name_index)
            continue
        if columns is None:
            continue
        ci, ki, ni = columns
        if ci >= len(row) or row[ci] in (None, ''):
            continue
        try:
            cost = Decimal(str(row[ci]).replace(' ', '').replace('\u00a0', '').replace(',', '.'))
        except InvalidOperation:
            continue
        if not cost.is_finite() or cost <= 0:
            continue
        keys = []
        if ki is not None and ki < len(row) and row[ki]:
            keys.append(identifier(row[ki]))
        if ni is not None and ni < len(row) and row[ni]:
            keys.append('name:' + normalize_name(str(row[ni])))
        for key in keys:
            if key in values and values[key] != cost:
                raise ValueError(f'В таблице несколько себестоимостей для {key}')
            values[key] = cost
    if columns is None:
        raise ValueError(f'Не найдены заголовки себестоимости и товара: {path}')
    return values


def build_plan(source, snapshot, stocks, costs, database_costs=None, reference='regular'):
    matcher = PriceMatcher(snapshot['products'])
    result, peers, short_names = [], [], set()
    for item in source:
        hit = matcher.match(item)
        mp = hit.product
        cost = costs.get(item['sku']) or costs.get('name:' + normalize_name(item['name']))
        if cost is None:
            cost = (database_costs or {}).get(item['sku'])
        cost = Decimal(str(cost)) if cost is not None else None
        raw = (mp.get('discountPrice') or mp.get('price')) if mp and reference == 'discount' else (
            mp.get('price') if mp else None)
        prices = reference_prices(raw, purchase_price=cost)
        name_1c = item['name'][:100]
        if name_1c.casefold() in short_names:
            name_1c = item['name'][:87] + f" [{item['source_row']}]"
        short_names.add(name_1c.casefold())
        row = {**item, 'name_1c': name_1c, 'quantity': stocks.get(item['barcode'], 0),
               'cost': cost, 'group': product_group(item['name']), 'reference': reference,
               'status': 'moba' if prices['base'] else hit.reason,
               'match_reason': hit.reason, 'candidates': hit.candidates,
               'moba': mp, 'reference_price': raw, 'retail': prices['base'], 'opt': prices['opt'], 'super_opt': prices['super_opt'], 'pickup': None}
        if row['retail'] and cost is not None:
            peers.append(PricingPeer(cost, Decimal(str(raw)), row['group'], item['quality']))
        result.append(row)
    for row in result:
        if row['retail'] is None:
            estimate = estimate_price(row['cost'], row['group'], row['quality'], peers)
            if estimate:
                row.update(reference_price=estimate.retail, retail=reference_prices(estimate.retail, purchase_price=row['cost'])['base'], status='estimated', multiplier=estimate.multiplier,
                           peer_count=estimate.sample_size, estimate_scope=estimate.scope)
            elif row['cost'] is None:
                row['status'] = 'needs_cost'
        if row['retail'] is not None:
            row['pickup'] = row['retail']
            row['opt'] = reference_prices(row.get('reference_price'), purchase_price=row['cost'])['opt']
            row['super_opt'] = reference_prices(row.get('reference_price'), purchase_price=row['cost'])['super_opt']
    return {'created_at': datetime.now(timezone.utc).isoformat(), 'reference': reference,
            'snapshot_date': snapshot['fetched_at'], 'snapshot_complete': snapshot['complete'],
            'source': 'https://moba.ru/', 'summary': {
                'products': len(result), 'moba_products': len(snapshot['products']),
                'status': dict(Counter(x['status'] for x in result)),
                'stock_products': len(stocks), 'stock_units': sum(stocks.values()),
                'stock_with_price': sum(x['quantity'] > 0 and x['retail'] is not None for x in result),
                'costs_known': sum(x['cost'] is not None for x in result)}, 'products': result}


def apply_plan(db, plan, backup_path: Path, stock_city: str | None = None,
               move_stock_from: str | None = None, create_stock_city_name: str | None = None):
    """Одна транзакция. Нет новых приходов при повторном запуске."""
    products = {p.sku: p for p in db.execute(select(Product)).scalars()}
    references = {p.sku: p for p in db.execute(select(MobaPrice)).scalars()}
    categories = list(db.execute(select(Category)).scalars())
    if not categories:
        raise ValueError('Сначала загрузите категории')
    first_word = {}
    for product in products.values():
        first_word.setdefault(product.name.split()[0].casefold(), product.category_id)
    city, origin, created_city = None, None, False
    if (move_stock_from or create_stock_city_name) and not stock_city:
        raise ValueError('Для перемещения или создания склада укажите stock_city')
    if stock_city:
        city = db.execute(select(City).where(City.slug == stock_city)).scalar_one_or_none()
        if city is None and create_stock_city_name:
            city = City(name=create_stock_city_name, slug=stock_city, is_active=True)
            db.add(city)
            db.flush()
            created_city = True
        if city is None:
            raise ValueError(f'Склад {stock_city} отсутствует. Создайте или укажите нужный склад.')
    if move_stock_from:
        origin = db.execute(select(City).where(City.slug == move_stock_from)).scalar_one_or_none()
        if origin is None or origin.id == city.id:
            raise ValueError('Укажите существующий другой склад-источник')
    keys = {p['sku'] for p in plan['products']}
    backup = {'created_at': datetime.now(timezone.utc).isoformat(), 'products': [], 'prices': [], 'stocks': []}
    for table, records, fields in (
        ('products', products.values(), ('sku', 'name', 'model', 'purchase_price', 'price_base', 'price_card', 'price_opt', 'price_super_opt')),
        ('prices', references.values(), ('sku', 'name', 'price', 'discount_price', 'our_base', 'our_card', 'our_opt', 'our_super_opt', 'url', 'link', 'parsed_at'))):
        backup[table] = [{key: getattr(record, key) for key in fields} for record in records if record.sku in keys]
    cities = [c.id for c in (city, origin) if c]
    all_stock = list(db.execute(select(Stock).where(Stock.city_id.in_(cities)).with_for_update()).scalars()) if cities else []
    stock_rows = {s.product_id: s for s in all_stock if s.city_id == city.id} if city else {}
    origin_rows = {s.product_id: s for s in all_stock if origin and s.city_id == origin.id}
    if origin:
        for row in plan['products']:
            if row['barcode'] not in plan.get('stock_barcodes', []):
                continue
            p = products.get(row['sku'])
            source_qty = origin_rows[p.id].quantity if p and p.id in origin_rows else 0
            target_qty = stock_rows[p.id].quantity if p and p.id in stock_rows else 0
            if source_qty < 0 or target_qty < 0 or source_qty + target_qty != row['quantity']:
                raise ValueError(f"Остатки {row['sku']} изменились: {source_qty}+{target_qty} != {row['quantity']}. Перемещение отменено.")
    backup['stocks'] = [{'product_id': s.product_id, 'city_id': s.city_id, 'quantity': s.quantity} for s in all_stock]
    backup['created_city'] = {'slug': city.slug, 'name': city.name} if created_city else None
    old_config = db.execute(select(SystemConfig).where(SystemConfig.key == 'BASE_FROM')).scalar_one_or_none()
    backup['base_from'] = old_config.value if old_config else None
    backup['created_products'] = [x['sku'] for x in plan['products'] if x['sku'] not in products]
    backup['created_prices'] = [x['sku'] for x in plan['products'] if x['sku'] not in references]
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    with backup_path.open('x') as backup_file:
        backup_file.write(json.dumps(backup, ensure_ascii=False, default=str))
    stats = Counter()
    for row in plan['products']:
        product = products.get(row['sku'])
        if product is None:
            category_id = first_word.get(row['name'].split()[0].casefold(), categories[0].id)
            product = Product(sku=row['sku'], name=row['name'], slug=row['sku'].lower(), category_id=category_id,
                              model=row['model'] or None, is_active=1)
            db.add(product)
            db.flush()
            products[product.sku] = product
            stats['created_products'] += 1
        if row['cost'] is not None:
            product.purchase_price = row['cost']
        if row['model']:
            product.model = row['model']
        mp = references.get(product.sku)
        if mp is None:
            mp = MobaPrice(sku=product.sku, name=product.name, price=0)
            db.add(mp)
            references[mp.sku] = mp
        if row['retail'] is not None:
            prices = reference_prices(row.get('reference_price') or Decimal(str(row['retail'])) / Decimal('1.2'), purchase_price=product.purchase_price)
            mp.name = product.name
            mp.our_base, mp.our_card, mp.our_opt = prices['base'], prices['card'], prices['opt']
            product.price_base, product.price_card, product.price_opt = prices['base'], prices['card'], prices['opt']
            mp.our_super_opt = product.price_super_opt = prices['super_opt']
            source = row['moba'] if row['status'] == 'moba' else None
            mp.price = Decimal(str(source.get('price') or 0)) if source else row.get('reference_price')
            mp.discount_price = Decimal(str(source['discountPrice'])) if source and source.get('discountPrice') else None
            mp.url = urljoin('https://moba.ru', source['link']) if source else None
            mp.link = mp.url
            mp.parsed_at = datetime.now(timezone.utc)
            stats['prices_updated'] += 1
        if city and row['barcode'] and row['barcode'] in plan.get('stock_barcodes', []):
            stock = stock_rows.get(product.id)
            old = stock.quantity if stock else 0
            difference = row['quantity'] - old
            if difference:
                if stock is None:
                    stock = Stock(product_id=product.id, city_id=city.id, quantity=0)
                    db.add(stock)
                    stock_rows[product.id] = stock
                stock.quantity = row['quantity']
                if origin:
                    origin_rows[product.id].quantity = 0
                    db.add(InventoryTransaction(product_id=product.id, type=TransactionType.TRANSFER,
                                                quantity=difference, from_city_id=origin.id, to_city_id=city.id,
                                                unit_cost=row['cost'], notes='Исправление склада stock.xlsx: ' + plan['created_at']))
                    stats['stock_transfers'] += 1
                    stats['stock_units_moved'] += difference
                else:
                    db.add(InventoryTransaction(product_id=product.id, type=TransactionType.ADJUSTMENT,
                                                quantity=difference, to_city_id=city.id, unit_cost=row['cost'],
                                                notes='Сверка stock.xlsx: ' + plan['created_at']))
                    stats['stock_adjustments'] += 1
    config = db.execute(select(SystemConfig).where(SystemConfig.key == 'BASE_FROM')).scalar_one_or_none()
    if config is None:
        db.add(SystemConfig(key='BASE_FROM', value=plan['reference'], description='Цена Moba для розницы'))
    else:
        config.value = plan['reference']
    db.flush()
    return dict(stats)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--catalog', type=Path, default=Path('catalog.xlsx'))
    ap.add_argument('--stock', type=Path, default=Path('stock.xlsx'))
    ap.add_argument('--costs', type=Path, action='append', default=[])
    ap.add_argument('--snapshot', type=Path, required=True)
    ap.add_argument('--output-plan', type=Path, required=True)
    ap.add_argument('--reference', choices=['regular', 'discount'], default='regular')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--stock-city')
    ap.add_argument('--move-stock-from', help='Переместить подтверждённый остаток с ошибочного склада, сохранив общее количество')
    ap.add_argument('--create-stock-city-name', help='Название нового склада, если stock-city ещё отсутствует')
    args = ap.parse_args()
    snapshot = json.loads(args.snapshot.read_text())
    if snapshot.get('complete') is not True or snapshot.get('errors'):
        raise SystemExit('Снимок Moba неполный; цены не изменены')
    costs = {}
    for file in args.costs:
        incoming = read_costs(file)
        if any(key in costs and costs[key] != value for key, value in incoming.items()):
            raise ValueError('В закупочных файлах расходятся значения себестоимости одного товара')
        costs.update(incoming)
    stocks = read_stocks(args.stock)
    with SessionLocal() as db:
        known_costs = {p.sku: p.purchase_price for p in db.execute(select(Product)).scalars() if p.purchase_price is not None}
        plan = build_plan(read_catalog(args.catalog), snapshot, stocks, costs, known_costs, args.reference)
        plan['stock_barcodes'] = sorted(stocks)
        args.output_plan.parent.mkdir(parents=True, exist_ok=True)
        args.output_plan.write_text(json.dumps(plan, ensure_ascii=False, default=str))
        print(json.dumps(plan['summary'], ensure_ascii=False))
        if args.apply:
            backup = args.output_plan.with_name(args.output_plan.stem + '-before.json')
            if backup.exists():
                raise SystemExit(f'Существующая резервная копия не перезаписывается: {backup}')
            stats = apply_plan(db, plan, backup, args.stock_city, args.move_stock_from, args.create_stock_city_name)
            db.commit()
            print('APPLIED ' + json.dumps(stats, ensure_ascii=False))
            print('BACKUP ' + str(backup))
        else:
            db.rollback()
            print('DRY RUN: БД не изменена')


if __name__ == '__main__':
    main()
