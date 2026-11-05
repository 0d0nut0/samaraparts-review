"""Розница = Moba × 1.2, опт = Moba, суперопт с защитой закупки."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, ROUND_CEILING
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import MobaPrice, Product


def round_money(value: Decimal) -> Decimal:
    return Decimal(str(value)).quantize(Decimal('1'), rounding=ROUND_HALF_UP)


def _positive(value) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return number if number.is_finite() and number > 0 else None


def reference_prices(source, opt_coefficient=None, *, purchase_price=None) -> dict:
    """Источник — обычная Moba. Граница закупка+15% округляется вверх.

    Без закупки суперопт неизвестен. Старый коэффициент не используется.
    """
    value = _positive(source)
    if value is None or round_money(value) <= 0:
        return dict(base=None, card=None, opt=None, super_opt=None)
    opt = round_money(value)
    retail = round_money(opt * Decimal('1.20'))
    cost = _positive(purchase_price)
    super_opt = None
    if cost is not None:
        floor = (cost * Decimal('1.15')).quantize(Decimal('1'), rounding=ROUND_CEILING)
        super_opt = max(round_money(opt * Decimal('0.90')), floor)
    return dict(base=retail, card=retail, opt=opt, super_opt=super_opt)


def buyer_prices(mp, wholesale: bool, *, authenticated=False, pickup=False, price_type=None) -> dict:
    """Одобренный тариф аккаунта; способ доставки не меняет цену."""
    base = _positive(mp.our_base)
    opt = _positive(mp.our_opt)
    super_opt = _positive(getattr(mp, 'our_super_opt', None))
    tier = price_type if authenticated and price_type else ('wholesale' if wholesale and authenticated else 'retail')
    if not authenticated:
        tier = 'retail'
    value = base
    if tier == 'super_wholesale' and super_opt:
        value = super_opt
    elif tier in ('wholesale', 'super_wholesale') and opt:
        value, tier = opt, 'wholesale'
    else:
        tier = 'retail'
    value = round_money(value) if value else None
    return dict(price=value, card=value, regular_price=value, pickup_price=value,
                discount=False, wholesale=tier != 'retail', price_type=tier)


async def calculate_prices(db: AsyncSession, moba_price: MobaPrice, *, purchase_price=None) -> dict:
    if purchase_price is None and db is not None:
        purchase_price = (await db.execute(select(Product.purchase_price).where(Product.sku == moba_price.sku))).scalar_one_or_none()
    return reference_prices(moba_price.price, purchase_price=purchase_price)


def assign_prices(mp, product, values):
    mp.our_base, mp.our_card, mp.our_opt, mp.our_super_opt = (
        values['base'], values['card'], values['opt'], values['super_opt'])
    product.price_base, product.price_card, product.price_opt, product.price_super_opt = (
        values['base'], values['card'], values['opt'], values['super_opt'])


async def recalculate_all(db: AsyncSession) -> int:
    rows = (await db.execute(select(MobaPrice, Product).join(Product, Product.sku == MobaPrice.sku)
                            .where(MobaPrice.url.isnot(None)))).all()
    for mp, product in rows:
        assign_prices(mp, product, reference_prices(mp.price, purchase_price=product.purchase_price))
    await db.commit()
    return len(rows)


def invalidate_config_cache():
    pass  # фиксированная политика вместо прежних коэффициентов
