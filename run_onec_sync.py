"""1С → товары/последняя закупка → цены Moba → установка цен в 1С.

Без --apply выполняет расчёт в транзакции с откатом, без записи в 1С.
--cached-source использует снимок var/onec-api только для диагностики.
"""
import argparse
import asyncio
import fcntl
import hashlib
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from decimal import Decimal

from sqlalchemy import select, delete, func, text

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import Product, MobaPrice, Stock, UserCart, Order, OrderItem, InventoryTransaction
from app.services import onec_exchange
from app.services.catalog_sync import sync_snapshot
from app.services.onec_odata import (OneCClient, TIERS, PRICE_DOC, commerce_xml,
                                     purchase_costs, add_purchase_price_overrides,
                                     changed_price_rows, price_document)
from scripts.sync_catalog_prices import read_catalog
from app.services.onec_inventory import apply_stock_snapshot


def save_private(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, default=str, indent=2))
    temporary.chmod(0o600)
    temporary.replace(path)


async def prepare(snapshot, moba, *, apply, folder, stamp, source_changed, reset_test_site=False):
    async with AsyncSessionLocal() as db:
        cleared = {}
        if reset_test_site:
            if not apply or not settings.ONEC_SOURCE_ONLY or not settings.ONEC_IMPORT_STOCKS or not snapshot.get('stock',{}).get('complete'):
                raise ValueError('Site reset requires a live complete 1C source and stock import')
            if db.bind.dialect.name == 'postgresql':
                await db.execute(text('LOCK TABLE orders, order_items, inventory_transactions, user_cart, stocks, products, moba_prices IN ACCESS EXCLUSIVE MODE'))
            for model in [OrderItem, Order, InventoryTransaction, UserCart, Stock, Product, MobaPrice]:
                cleared[model.__tablename__] = (await db.execute(select(func.count()).select_from(model))).scalar_one()
                await db.execute(delete(model))
        stocks = list((await db.execute(select(Stock.id, Stock.quantity).order_by(Stock.id))).all())
        costs, skipped = purchase_costs(snapshot['receipts'], snapshot['rub'])
        costs = add_purchase_price_overrides(costs, snapshot.get('purchase_prices', []), snapshot['rub'])
        report = dict(source_products=len(snapshot['catalog']), source_receipts=len(snapshot['receipts']),
                      purchase_costs=len(costs), ignored_purchase_documents=skipped)
        if reset_test_site:
            report['cleared_test_data'] = cleared
        if source_changed or not apply:
            names = json.loads(Path(settings.ONEC_CATALOG_MAP).read_text())
            before_products = list((await db.execute(select(Product))).scalars())
            linked_before = sum(bool(p.onec_id) for p in before_products)
            if apply:
                fields = ['id', 'sku', 'onec_id', 'name', 'is_active', 'purchase_price',
                          'price_moba', 'price_base', 'price_card', 'price_opt', 'price_super_opt']
                backup = {'products': [{k: getattr(p, k) for k in fields} for p in before_products]}
                price_fields = ['sku', 'price', 'our_base', 'our_card', 'our_opt', 'our_super_opt', 'url', 'link', 'parsed_at']
                backup['prices'] = [{k: getattr(p, k) for k in price_fields}
                                    for p in (await db.execute(select(MobaPrice))).scalars()]
                save_private(folder / f'before-site-{stamp}.json', backup)
            imported = await onec_exchange.import_catalog(db, commerce_xml(snapshot, names))
            if not linked_before and len(before_products) > 100 and imported.created > 50:
                raise ValueError('Initial 1C mapping would create too many duplicate products; review required')
            linked = {p.onec_id: p for p in (await db.execute(select(Product))).scalars() if p.onec_id}
            for row in snapshot['catalog']:
                key = row['Ref_Key']
                if key in linked:
                    linked[key].purchase_price = costs[key]['cost'] if key in costs else None
            await db.flush()
            aliases = {p['sku']: p for p in read_catalog(Path('catalog.xlsx'))} if Path('catalog.xlsx').exists() else {}
            if settings.ONEC_SOURCE_ONLY:
                # Old Excel names are reference aliases only. Goods and quantities
                # are created solely from the actual 1C catalogue.
                aliases = {p.sku:aliases[names[p.name.casefold()]] for p in linked.values()
                           if names.get(p.name.casefold()) in aliases}
            pricing = await db.run_sync(lambda session: sync_snapshot(session, moba, apply=True, aliases=aliases))
            await db.flush()
            after = list((await db.execute(select(Stock.id, Stock.quantity).order_by(Stock.id))).all())
            if stocks != after:
                raise ValueError('Inventory changed unexpectedly; transaction rolled back')
            report.update(created=imported.created, linked_or_updated=imported.updated,
                          costs_applied=sum(k in linked for k in costs), pricing=pricing['summary'])
        else:
            report['source_unchanged'] = True
        feed = await onec_exchange.price_feed(db)
        report['priced_products'] = len(feed['items'])
        report['super_prices'] = sum(p['super_wholesale'] is not None for p in feed['items'])
        if settings.ONEC_IMPORT_STOCKS:
            report.update(await apply_stock_snapshot(db, snapshot['stock']))
        if apply:
            await db.commit()
        else:
            await db.rollback()
        return feed['items'], report


def return_prices(client, rows, *, rub, folder, stamp, price_table):
    documents = []
    # 1C cannot compare its unlimited-length Comment field in an OData filter.
    # Fetch only document headers, then match our deterministic marker locally.
    headers = client.rows(PRICE_DOC, {'$filter': 'DeletionMark eq false',
                                     '$select': 'Ref_Key,Number,Posted,Date,Комментарий'}) if rows else []
    for start in range(0, len(rows), 1000):
        batch = rows[start:start + 1000]
        digest = hashlib.sha256(json.dumps([price_table, batch], ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]
        comment = 'PARTS sync ' + digest
        existing = [d for d in headers if d.get('Комментарий') == comment]
        if len(existing) > 1:
            raise ValueError('Duplicate owned 1C price documents; review required')
        document = existing[0] if existing else client.write(PRICE_DOC, price_document(batch, comment=comment, price_table=price_table))
        key = document['Ref_Key']
        save_private(folder / f'price-document-{key}.json', {'id': key, 'comment': comment, 'rows': batch})
        resource = f"{PRICE_DOC}(guid'{key}')"
        if not document.get('Posted'):
            client.write(resource + '/Post(PostingModeOperational=false)')
        verified = client.get(resource, {'$select': 'Ref_Key,Number,Posted,Date,Комментарий'})
        if not verified.get('Posted'):
            raise ValueError('1C did not post its price document')
        actual = client.current_prices()
        unresolved = []
        for row in batch:
            got = actual.get((row['Номенклатура_Key'], row['ВидЦены_Key']))
            if (not got or Decimal(str(got['Цена'])) != Decimal(row['Цена'])
                    or got.get('Валюта_Key') != rub):
                unresolved.append(row['Номенклатура_Key'])
        if unresolved:
            raise ValueError(f'Price document {verified.get("Number")} is posted but {len(unresolved)} register prices differ; check price table {price_table}')
        documents.append({'id': key, 'number': verified.get('Number'), 'rows': len(batch)})
        print('1C price document verified', verified.get('Number'), 'rows', len(batch), flush=True)
    return documents


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--cached-source', action='store_true')
    parser.add_argument('--reset-test-site', action='store_true', help='Delete site test goods, stock, carts and order history inside the import transaction; requires --apply')
    parser.add_argument('--price-table', choices=['Товары', 'Товары2_5'], default='Товары2_5')
    args = parser.parse_args()
    if args.reset_test_site and (not args.apply or args.cached_source):
        parser.error('--reset-test-site requires --apply and a live 1C snapshot')
    folder = Path('var/onec-sync'); folder.mkdir(parents=True, exist_ok=True)
    lock = (folder / 'sync.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print('1C sync is already running')
        return
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    state_file = folder / 'state.json'
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    moba = json.loads(Path(settings.MOBA_SNAPSHOT_PATH).read_text())
    age = datetime.now(timezone.utc) - datetime.fromisoformat(moba['fetched_at'])
    if not moba.get('complete') or moba.get('errors') or not timedelta(0) <= age < timedelta(hours=24):
        raise ValueError('A complete Moba snapshot less than 24 hours old is required')
    client = OneCClient(settings.ONEC_ODATA_URL, settings.ONEC_ODATA_LOGIN, settings.ONEC_ODATA_PASSWORD)
    try:
        if args.cached_source:
            source = {name: json.loads((Path('var/onec-api') / f'{name}.json').read_text())['value']
                      for name in ['catalog', 'receipts', 'barcodes']}
            currencies = json.loads(Path('var/onec-api/currencies.json').read_text())['value']
            rub = [c['Ref_Key'] for c in currencies if c.get('Code') == '643']
            if len(rub) != 1:
                raise ValueError('Cached RUB currency is ambiguous')
            source['rub'] = rub[0]
        else:
            source = client.snapshot()
            save_private(folder / 'latest-source.json', source)
        catalog_source = {key:value for key,value in source.items() if key != 'stock'}
        fingerprint = hashlib.sha256(json.dumps([catalog_source, moba['fetched_at'], settings.ONEC_SOURCE_ONLY], sort_keys=True).encode()).hexdigest()
        print('1C source fetched', len(source['catalog']), 'products,', len(source['receipts']), 'receipts', flush=True)
        items, report = asyncio.run(prepare(source, moba, apply=args.apply, folder=folder, stamp=stamp,
                                           source_changed=args.reset_test_site or state.get('fingerprint') != fingerprint,
                                           reset_test_site=args.reset_test_site))
        if args.reset_test_site:
            from app.services.cart import redis_client
            for key in redis_client.scan_iter('cart:*'):
                redis_client.delete(key)
        if args.apply:
            type_ids = client.ensure_types(source['rub'])
        else:
            existing = {t['Description']: t['Ref_Key'] for t in client.price_types()}
            type_ids = {tier: existing.get(name, 'missing:' + name) for name, tier in TIERS.items()}
        current = client.current_prices()
        rows = changed_price_rows(items, type_ids, current, source['rub'])
        report.update(applied=args.apply, changed_price_rows=len(rows), price_types=type_ids)
        save_private(folder / f'plan-{stamp}.json', {'summary': report, 'rows': rows})
        if args.apply:
            save_private(folder / f'before-1c-prices-{stamp}.json', list(current.values()))
            report['documents'] = return_prices(client, rows, rub=source['rub'], folder=folder,
                                                stamp=stamp, price_table=args.price_table)
            report['verified'] = True
            save_private(state_file, {'fingerprint': fingerprint, 'completed_at': datetime.now(timezone.utc).isoformat(), 'summary': report})
        save_private(folder / 'latest-result.json', report)
        print(json.dumps(report, ensure_ascii=False, default=str), flush=True)
    finally:
        client.close()
        lock.close()


if __name__ == '__main__':
    main()
