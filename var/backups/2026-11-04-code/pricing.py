"""Цены в целых рублях: розница Moba, −10% за самовывоз после входа."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MobaPrice, Product, SystemConfig

# Простой локальный кэш для коэффициентов, чтобы не ходить в БД на каждый запрос
_config_cache = {}

async def get_config_value(db: AsyncSession, key: str, default: str) -> str:
    """Получает значение настройки из БД или возвращает дефолт."""
    global _config_cache
    if key in _config_cache:
        return _config_cache[key]

    result = await db.execute(select(SystemConfig.value).where(SystemConfig.key == key))
    val = result.scalar_one_or_none()

    final_val = val if val is not None else default
    _config_cache[key] = final_val
    return final_val

def round_money(value: Decimal) -> Decimal:
    """Округляет цену единицы до целого рубля: 13,50 → 14."""
    return Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def _positive(value) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return number if number.is_finite() and number > 0 else None


def reference_prices(source, opt_coefficient=Decimal("0.9")) -> dict:
    """Полная розница равна выбранной цене Moba; по карте она та же.

    Скидка за самовывоз применяется только в buyer_prices, чтобы она не
    вычиталась дважды. Оптовая цена остаётся внутренним параметром.
    """
    value = _positive(source)
    regular = round_money(value) if value is not None else None
    if regular is None or regular <= 0:
        return {"base": None, "card": None, "opt": None}
    opt_ratio = _positive(opt_coefficient) or Decimal("0.9")
    return {"base": regular, "card": regular, "opt": round_money(regular * opt_ratio)}


def buyer_prices(mp, wholesale: bool, *, authenticated: bool = False, pickup: bool = False) -> dict:
    """Единая публичная цена; 10% за самовывоз доступны только после входа.

    Историческое our_card хранит полную розничную цену. Если оно не заполнено,
    используем our_base.
    Оптовый статус сохраняет цену our_opt; скидка за самовывоз применяется к цене покупателя.
    """
    base = _positive(mp.our_base)
    retail = _positive(mp.our_card) or base
    if base is None or retail is None or round_money(retail) <= 0:
        return {"price": None, "card": None, "regular_price": None, "pickup_price": None,
                "discount": False, "wholesale": False}
    opt = _positive(mp.our_opt)
    is_wholesale = bool(wholesale and authenticated and opt and opt > 0)
    regular = round_money(opt if is_wholesale else retail)
    pickup_price = round_money(regular * Decimal("0.90"))
    discounted = bool(authenticated and pickup)
    price = pickup_price if discounted else regular
    # card — совместимость со старыми внутренними потребителями, отдельной публичной цены нет.
    return {"price": price, "card": price, "regular_price": regular, "pickup_price": pickup_price,
            "discount": discounted, "wholesale": is_wholesale}

async def calculate_prices(db: AsyncSession, moba_price: MobaPrice) -> dict:
    """Выбирает обычную/дисконтную цену Moba без прежней цепочки ×0,9×1,1."""
    base_from = await get_config_value(db, "BASE_FROM", "regular")
    coef_opt = Decimal(await get_config_value(db, "COEF_OPT", "0.9"))

    if base_from == "discount":
        source = _positive(moba_price.discount_price) or moba_price.price
    else:
        source = moba_price.price

    return reference_prices(source, coef_opt)

async def recalculate_all(db: AsyncSession) -> int:
    """
    Пересчитывает цены товаров, загруженных парсером Мобы (у них заполнен url).
    Цены поставок (scripts/import_supply.py) и товаров, созданных в админке, задаются вручную —
    формула «цена Мобы × коэффициент» к ним не относится, их не трогаем.
    """
    records = await db.execute(select(MobaPrice).where(MobaPrice.url.isnot(None)))
    all_records = records.scalars().all()
    products = (await db.execute(select(Product).where(
        Product.sku.in_([record.sku for record in all_records])
    ))).scalars().all() if all_records else []
    by_sku = {product.sku: product for product in products}
    updated = 0

    for record in all_records:
        prices = await calculate_prices(db, record)
        record.our_base = prices["base"]
        record.our_card = prices["card"]
        record.our_opt = prices["opt"]
        product = by_sku.get(record.sku)
        if product:
            product.price_base, product.price_card, product.price_opt = (
                prices["base"], prices["card"], prices["opt"])
        updated += 1

    await db.commit()
    return updated

def invalidate_config_cache():
    """Очищает кэш настроек при их изменении в админке."""
    global _config_cache
    _config_cache = {}


if __name__ == "__main__":
    # Массовый пересчёт: python -m app.services.pricing
    import asyncio
    import sys

    from app.database import AsyncSessionLocal

    async def _main() -> None:
        async with AsyncSessionLocal() as session:
            print(f"Пересчитано цен: {await recalculate_all(session)}")

    # psycopg async на Windows работает только с SelectorEventLoop
    asyncio.run(_main(), loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None)
