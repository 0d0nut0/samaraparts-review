"""Сопоставляет публичный снимок Moba только с нашим каталогом, без создания чужих товаров."""
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import select
from app.models import Product, MobaPrice
from app.services.pricing import reference_prices, assign_prices, _positive
from app.services.price_matching import PriceMatcher, PricingPeer, estimate_price, product_group


def sync_snapshot(db, snapshot, *, apply=False, aliases=None):
    if not snapshot.get('complete') or snapshot.get('errors'):
        raise ValueError('Неполный снимок Moba: цены не меняются')
    source = snapshot.get('products', [])
    if not source:
        raise ValueError('Пустой снимок Moba')
    matcher = PriceMatcher(source)
    alias_map = aliases or {}
    prices = {m.sku: m for m in db.scalars(select(MobaPrice))}
    products = list(db.scalars(select(Product)))
    by_link = {}
    for row in source:
        key = str(row.get('link', '')).split('?')[0].rstrip('/')
        if key:
            by_link.setdefault(key, []).append(row)
    planned, peers = [], []
    for p in products:
        mp = prices.get(p.sku)
        identity = {**alias_map.get(p.sku, {}), 'name': p.name}
        # A previously confirmed URL keeps identity even after a private-label name change.
        link = str(mp.link or mp.url or '') if mp else ''
        link = link.removeprefix('https://moba.ru').split('?')[0].rstrip('/')
        linked = by_link.get(link, [])
        hit = matcher.match(identity)
        match = linked[0] if len(linked) == 1 else hit.product
        value = _positive(match.get('price')) if match else None
        entry = dict(product=p, mp=mp, source=match, reference=value,
                     status='moba' if value else hit.reason, quality=identity.get('quality', ''),
                     group=product_group(p.name))
        planned.append(entry)
        if value and _positive(p.purchase_price):
            peers.append(PricingPeer(Decimal(p.purchase_price), value, entry['group'], entry['quality']))
    result = []
    for row in planned:
        p, mp, value = row['product'], row['mp'], row['reference']
        if value is None and mp and mp.url and _positive(mp.price):
            value, row['status'] = mp.price, 'stale_moba'
        if value is None:
            estimate = estimate_price(_positive(p.purchase_price), row['group'], row['quality'], peers)
            if estimate:
                value, row['status'] = estimate.retail, 'estimated'
            elif not _positive(p.purchase_price):
                row['status'] = 'needs_cost'
        values = reference_prices(value, purchase_price=p.purchase_price)
        if apply and not _positive(p.purchase_price):
            # A cancelled receipt must also revoke a formerly estimated SuperOPT.
            p.price_super_opt = None
            if mp:
                mp.our_super_opt = None
        result.append(dict(sku=p.sku, onec_id=p.onec_id, name=p.name,
                           status=row['status'], reference=str(value) if value else None,
                           cost=str(p.purchase_price) if p.purchase_price else None,
                           **{k: str(v) if v is not None else None for k,v in values.items()}))
        if apply and value:
            if mp is None:
                mp = MobaPrice(sku=p.sku, name=p.name, price=value)
                db.add(mp)
            mp.price = value
            mp.name = p.name
            p.price_moba = value
            if row['status'] == 'moba':
                src = row['source']
                link = str(src.get('link') or '')
                mp.link = mp.url = 'https://moba.ru' + link if link.startswith('/') else link
                mp.discount_price = _positive(src.get('discountPrice'))
            elif row['status'] != 'stale_moba':
                mp.link = mp.url = None
            if row['status'] != 'stale_moba':
                mp.parsed_at = datetime.now(timezone.utc)
            assign_prices(mp, p, values)
    return dict(fetched_at=snapshot.get('fetched_at'), policy='retail=opt*1.2; opt=moba_regular; super=max(opt*0.9,ceil(cost*1.15))',
                summary=dict(Counter(r['status'] for r in result)), products=result)
