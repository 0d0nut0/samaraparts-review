"""
Гибридная корзина: Redis для гостей, БД (user_cart) для авторизованных пользователей.

Функции, меняющие корзину в БД, делают commit сами — роутеры получают сессию
через get_db(), которая коммит не выполняет.
"""
from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid4

import redis
from sqlalchemy import select, delete, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import REDIS_URL
from app.models import MobaPrice, UserCart, Product
from app.services.pricing import buyer_prices

# Клиент Redis — синхронный, декодируем значения в строки
redis_client = redis.from_url(REDIS_URL, decode_responses=True)

CART_TTL = 60 * 60 * 24 * 30  # 30 дней
MAX_QTY = 999  # столько же разрешает поле количества на сайте


def get_cart_id(request) -> str:
    """Возвращает cart_id из сессии, создавая при отсутствии."""
    if "cart_id" not in request.session:
        request.session["cart_id"] = str(uuid4())
    return request.session["cart_id"]


def _redis_key(cart_id: str) -> str:
    return f"cart:{cart_id}"


def _is_wholesale(request) -> bool:
    user = getattr(request.state, "user", None)
    return bool(user and user.is_wholesale)


def _make_item(p: MobaPrice, qty: int, wholesale: bool, *, authenticated: bool = False, pickup: bool = False, price_type=None) -> dict:
    prices = buyer_prices(p, wholesale, authenticated=authenticated, pickup=pickup, price_type=price_type)
    price, card = prices["price"] or Decimal(0), prices["card"] or Decimal(0)
    return {
        "sku": p.sku,
        "name": p.name,
        "qty": qty,
        "price": price,
        "sum": price * qty,
        "price_card": card,
        "sum_card": card * qty,
        "wholesale": prices["wholesale"],
        "regular_price": prices["regular_price"] or Decimal(0),
        "regular_sum": (prices["regular_price"] or Decimal(0)) * qty,
        "discount": prices["discount"],
    }


async def user_cart_items(db: AsyncSession, user_id: int, wholesale: bool, *, pickup: bool = True, price_type=None) -> list[dict]:
    """Корзина аккаунта (сайт после входа и Telegram-бот — одна и та же корзина)."""
    stmt = (
        select(UserCart, MobaPrice)
        .join(Product, Product.id == UserCart.product_id)
        .join(MobaPrice, Product.sku == MobaPrice.sku)
        .where(UserCart.user_id == user_id, Product.is_active == 1, MobaPrice.our_base.isnot(None))
        .order_by(UserCart.id)
    )
    rows = (await db.execute(stmt)).all()
    return [_make_item(mp, ci.quantity, wholesale, authenticated=True, pickup=pickup, price_type=price_type) for ci, mp in rows]


async def get_cart_items(request, db: AsyncSession, *, pickup: bool = True) -> list[dict]:
    """Список позиций корзины: из БД для авторизованного пользователя, иначе из Redis."""
    wholesale = _is_wholesale(request)
    user_id = request.session.get("user_id")

    user = getattr(request.state, "user", None)
    if user_id and user and user.id == user_id:
        return await user_cart_items(db, user_id, wholesale, pickup=pickup, price_type=user.effective_price_type)

    raw = redis_client.hgetall(_redis_key(get_cart_id(request)))
    if not raw:
        return []
    result = await db.execute(
        select(MobaPrice)
        .join(Product, Product.sku == MobaPrice.sku)
        .where(MobaPrice.sku.in_(list(raw.keys())), Product.is_active == 1, MobaPrice.our_base.isnot(None))
    )
    by_sku = {p.sku: p for p in result.scalars().all()}

    items = []
    for sku, qty_str in raw.items():
        qty = int(qty_str)
        p = by_sku.get(sku)
        if p and qty > 0:
            items.append(_make_item(p, qty, wholesale))
    return items


async def _get_product(db: AsyncSession, sku: str) -> Product | None:
    """Товар, который можно купить: есть в базе, не скрыт и с ценой (не «Цена по запросу»)."""
    return (
        await db.execute(
            select(Product)
            .join(MobaPrice, MobaPrice.sku == Product.sku)
            .where(Product.sku == sku, Product.is_active == 1, MobaPrice.our_base.isnot(None))
        )
    ).scalar_one_or_none()


async def _get_cart_row(db: AsyncSession, user_id: int, product_id: int) -> UserCart | None:
    stmt = select(UserCart).where(UserCart.user_id == user_id, UserCart.product_id == product_id)
    return (await db.execute(stmt)).scalar_one_or_none()


async def user_add(db: AsyncSession, user_id: int, sku: str, qty: int) -> int:
    """Добавляет товар в корзину аккаунта; возвращает новое количество (0 — купить нельзя)."""
    if qty <= 0:
        return 0
    product = await _get_product(db, sku)
    if not product:
        return 0
    row = await _get_cart_row(db, user_id, product.id)
    qty = min(qty, MAX_QTY)
    if row:
        row.quantity = min(row.quantity + qty, MAX_QTY)
        new_qty = row.quantity
    else:
        db.add(UserCart(user_id=user_id, product_id=product.id, quantity=qty))
        new_qty = qty
    await db.commit()
    return new_qty


async def user_set(db: AsyncSession, user_id: int, sku: str, qty: int) -> int:
    """Точное количество в корзине аккаунта; qty <= 0 удаляет позицию."""
    qty = min(qty, MAX_QTY)
    # Количество можно задать только товару, который можно купить (иначе в корзину попадал
    # товар «Цена по запросу» и заказ оформлялся по 0 ₽)
    product = await _get_product(db, sku) if qty > 0 else (
        await db.execute(select(Product).where(Product.sku == sku))).scalar_one_or_none()
    if not product:
        return 0
    row = await _get_cart_row(db, user_id, product.id)
    if qty <= 0:
        if row:
            await db.delete(row)
        qty = 0
    elif row:
        row.quantity = qty
    else:
        db.add(UserCart(user_id=user_id, product_id=product.id, quantity=qty))
    await db.commit()
    return qty


async def user_clear(db: AsyncSession, user_id: int, commit: bool = True) -> None:
    await db.execute(delete(UserCart).where(UserCart.user_id == user_id))
    if commit:
        await db.commit()


async def user_count(db: AsyncSession, user_id: int) -> int:
    stmt = select(func.coalesce(func.sum(UserCart.quantity), 0)).where(UserCart.user_id == user_id)
    return int((await db.execute(stmt)).scalar() or 0)


async def add_to_cart(request, sku: str, qty: int, db: AsyncSession) -> int:
    """Добавляет товар и возвращает новое количество позиции (0 — товар не найден)."""
    if qty <= 0:
        return 0
    user_id = request.session.get("user_id")
    if user_id:
        return await user_add(db, user_id, sku, qty)

    # Скрытый товар или товар без цены («Цена по запросу») в корзину не кладём
    if not await _get_product(db, sku):
        return 0
    key = _redis_key(get_cart_id(request))
    current = int(redis_client.hget(key, sku) or 0)
    new_qty = min(current + qty, MAX_QTY)
    redis_client.hset(key, sku, new_qty)
    redis_client.expire(key, CART_TTL)
    return new_qty


async def set_quantity(request, sku: str, qty: int, db: AsyncSession) -> int:
    """Устанавливает точное количество; qty <= 0 удаляет позицию."""
    user_id = request.session.get("user_id")
    if user_id:
        return await user_set(db, user_id, sku, qty)

    qty = min(qty, MAX_QTY)
    key = _redis_key(get_cart_id(request))
    if qty <= 0:
        redis_client.hdel(key, sku)
        return 0
    if not await _get_product(db, sku):
        return 0
    redis_client.hset(key, sku, qty)
    redis_client.expire(key, CART_TTL)
    return qty


async def remove_from_cart(request, sku: str, db: AsyncSession) -> None:
    await set_quantity(request, sku, 0, db)


async def clear_cart_async(request, db: AsyncSession, commit: bool = True) -> None:
    """Очищает корзину. commit=False — когда очистка входит в чужую транзакцию (оформление заказа)."""
    user_id = request.session.get("user_id")
    if user_id:
        await user_clear(db, user_id, commit)
    else:
        redis_client.delete(_redis_key(get_cart_id(request)))


async def get_cart_count_async(request, db: AsyncSession) -> int:
    """Количество единиц товара в корзине (сумма qty — одинаково для Redis и БД)."""
    user_id = request.session.get("user_id")
    if user_id:
        return await user_count(db, user_id)
    raw = redis_client.hgetall(_redis_key(get_cart_id(request)))
    return sum(int(v) for v in raw.values())


def cart_totals(items: list[dict]) -> dict:
    """Итоги после поштучного округления цены, в точности как в сохранённом заказе."""
    total = sum((i["sum"] for i in items), Decimal("0"))
    total_card = sum((i["sum_card"] for i in items), Decimal("0"))
    regular = sum((i.get("regular_sum", i["sum"]) for i in items), Decimal("0"))
    return {
        "total": total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        "total_card": total_card.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        "count": sum(i["qty"] for i in items),
        "regular_total": regular,
        "discount_amount": regular - total,
        "pickup_discount": any(i.get("discount") for i in items),
    }
