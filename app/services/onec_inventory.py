"""Абсолютные доступные остатки 1С с сохранением ещё не переданных заказов сайта."""
from collections import defaultdict
from decimal import Decimal
from sqlalchemy import select
from app.models import Product, City, Stock, Order, OrderItem, InventoryTransaction
from app.models.order import OrderStatus
from app.models.inventory_transaction import TransactionType
from app.services.onec_odata import ZERO


def stock_quantities(snapshot, warehouse_to_city, product_ids):
    if not snapshot.get('complete') or not snapshot.get('warehouses'):
        raise ValueError('Incomplete 1C stock snapshot; stock is not changed')
    warehouses = {w['Ref_Key'] for w in snapshot['warehouses']}
    if not warehouses <= warehouse_to_city.keys():
        raise ValueError('Map every 1C warehouse to a site city before enabling stock import')
    result = defaultdict(int)
    for row in snapshot['balances']:
        wh, key = row['Склад_Key'], row['Номенклатура_Key']
        physical = Decimal(str(row['ВНаличииBalance']))
        reserved = Decimal(str(row.get('КОтгрузкеBalance', 0)))
        if any(not n.is_finite() or n != n.to_integral_value() for n in (physical, reserved)):
            raise ValueError('Non-integer or invalid quantity in 1C stock snapshot')
        if wh not in warehouse_to_city or key not in product_ids:
            raise ValueError('1C stock refers to an unmapped warehouse or product')
        if row.get('Характеристика_Key') not in (None, '', ZERO) and physical:
            raise ValueError('Product variants require separate mapping before stock import')
        result[(product_ids[key], warehouse_to_city[wh])] += max(0, int(physical - reserved))
    return dict(result)


async def apply_stock_snapshot(db, snapshot):
    products = {p.onec_id:p.id for p in (await db.execute(select(Product))).scalars() if p.onec_id}
    cities = list((await db.execute(select(City))).scalars())
    mapping = {c.onec_warehouse_id:c.id for c in cities if c.onec_warehouse_id}
    quantities = stock_quantities(snapshot, mapping, products)
    # Until 1C has accepted/reserved an order, keep its local stock hold.
    pending = (await db.execute(select(OrderItem.product_id, Order.city_id, OrderItem.quantity)
                               .join(Order, Order.id == OrderItem.order_id)
                               .where(Order.status != OrderStatus.CANCELLED, Order.onec_exported_at.is_(None)))).all()
    held = defaultdict(int)
    for product_id, city_id, quantity in pending:
        held[(product_id, city_id)] += quantity
    quantities = {key:max(0, value-held[key]) for key,value in quantities.items()}
    existing = {(s.product_id,s.city_id):s for s in (await db.execute(select(Stock).with_for_update())).scalars()}
    changes = 0
    for key in quantities.keys() | existing.keys():
        quantity = quantities.get(key, 0)
        row = existing.get(key)
        old = row.quantity if row else 0
        if quantity == old:
            continue
        if row:
            row.quantity = quantity
        else:
            db.add(Stock(product_id=key[0],city_id=key[1],quantity=quantity))
        db.add(InventoryTransaction(product_id=key[0],type=TransactionType.ADJUSTMENT,
                  quantity=quantity-old,to_city_id=key[1] if quantity>old else None,
                  from_city_id=key[1] if quantity<old else None,notes='Доступный остаток из 1С'))
        changes += 1
    await db.flush()
    return {'stock_changed_rows':changes,'stock_units':sum(quantities.values()),
            'stock_positive_rows':sum(q>0 for q in quantities.values()),'local_held_units':sum(held.values())}
