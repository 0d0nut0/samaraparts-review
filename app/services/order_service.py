"""
Сервис управления заказами.
Отвечает за атомарное создание заказов и управление остатками на складе.
"""
import logging
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.city import City
from app.models.inventory_transaction import InventoryTransaction, TransactionType
from app.models.order import Order, OrderItem, OrderStatus, OrderType
from app.models.product import Product
from app.models.stock import Stock

logger = logging.getLogger(__name__)


class OrderError(Exception):
    """Бизнес-ошибка оформления/изменения заказа (текст можно показывать пользователю)."""


async def create_order(
    db: AsyncSession,
    user_id: int | None,
    city_id: int,
    cart_items: list[dict],  # [{sku, qty, price}]
    customer_name: str,
    customer_phone: str,
    address: str | None = None,
    wholesale: bool = False,
    comment: str | None = None,
    source: str = "site",
) -> Order:
    """
    Создаёт заказ и атомарно списывает остатки со склада выбранного города.

    Транзакция: строки остатков блокируются SELECT ... FOR UPDATE, при любой ошибке
    выполняется rollback и ни одно списание не сохраняется. Коммит делает вызывающий
    код (чтобы в ту же транзакцию попала очистка корзины).
    """
    try:
        city = await db.get(City, city_id)
        if not city or not city.is_active:
            raise OrderError("Выберите склад для получения заказа")
        if not cart_items:
            raise OrderError("Корзина пуста")
        total_amount = Decimal("0.00")
        profit = Decimal("0.00")
        all_costs_known = True
        order = Order(
            user_id=user_id,
            city_id=city_id,
            total_amount=0,
            status=OrderStatus.PENDING,
            order_type=OrderType.WHOLESALE if wholesale else OrderType.RETAIL,
            customer_name=customer_name,
            customer_phone=customer_phone,
            address=address,
            comment=comment,
            source=source,
        )
        db.add(order)

        for item in cart_items:
            sku, qty = item["sku"], int(item["qty"])
            product = (await db.execute(select(Product).where(Product.sku == sku))).scalar_one_or_none()
            if not product:
                raise OrderError(f"Товар {sku} не найден в базе")
            if not product.is_active:
                raise OrderError(f"Товар «{product.name}» снят с продажи — удалите его из корзины")
            if qty <= 0:
                raise OrderError("Некорректное количество товара")
            if Decimal(item["price"]) <= 0:
                raise OrderError(f"У товара «{product.name}» нет цены — уточните её у менеджера в чате")

            stock = (
                await db.execute(
                    select(Stock)
                    .where(Stock.product_id == product.id, Stock.city_id == city_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if not stock or stock.quantity < qty:
                available = stock.quantity if stock else 0
                raise OrderError(f"Недостаточно товара «{product.name}» в выбранном городе (доступно: {available} шт.)")

            stock.quantity -= qty
            price = Decimal(item["price"])
            total_amount += price * qty
            if product.purchase_price is not None:
                profit += (price - Decimal(product.purchase_price)) * qty
            else:
                all_costs_known = False

            order.items.append(OrderItem(product_id=product.id, quantity=qty, price_at_time=price))
            db.add(InventoryTransaction(
                product_id=product.id, user_id=user_id, type=TransactionType.SALE,
                quantity=-qty, from_city_id=city_id,
            ))

        order.total_amount = total_amount
        order.profit = profit if all_costs_known else None
        await db.flush()  # получаем order.id
        return order
    except OrderError:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        logger.exception("create_order failed")
        raise OrderError("Внутренняя ошибка при оформлении заказа")


async def change_order_status(db: AsyncSession, order_id: int, new_status: OrderStatus, staff_id: int | None) -> Order:
    """
    Меняет статус заказа с учётом склада:
    - переход в CANCELLED возвращает товар на склад города заказа;
    - выход из CANCELLED снова списывает товар (если его хватает).
    Коммит делает вызывающий код.
    """
    order = (
        await db.execute(select(Order).options(selectinload(Order.items)).where(Order.id == order_id))
    ).scalar_one_or_none()
    if not order:
        raise OrderError("Заказ не найден")
    old_status = order.status
    if old_status == new_status:
        return order

    if new_status == OrderStatus.CANCELLED or old_status == OrderStatus.CANCELLED:
        sign = 1 if new_status == OrderStatus.CANCELLED else -1  # +1 вернуть на склад, -1 списать заново
        for it in order.items:
            stock = (
                await db.execute(
                    select(Stock)
                    .where(Stock.product_id == it.product_id, Stock.city_id == order.city_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if stock is None:
                stock = Stock(product_id=it.product_id, city_id=order.city_id, quantity=0)
                db.add(stock)
            if sign < 0 and stock.quantity < it.quantity:
                await db.rollback()
                raise OrderError("Нельзя восстановить заказ: товара на складе уже недостаточно")
            stock.quantity += sign * it.quantity
            db.add(InventoryTransaction(
                product_id=it.product_id, user_id=staff_id,
                type=TransactionType.RETURN if sign > 0 else TransactionType.SALE,
                quantity=sign * it.quantity,
                to_city_id=order.city_id if sign > 0 else None,
                from_city_id=order.city_id if sign < 0 else None,
                notes=f"{'Отмена' if sign > 0 else 'Восстановление'} заказа #{order.id}",
            ))

    order.status = new_status
    return order
