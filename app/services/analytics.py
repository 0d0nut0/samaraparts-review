"""
Аналитика админ-панели (перенесено из админки Telegram-бота PartsBot):
сводка склада, KPI продаж, ABC-анализ, прогноз закупок, движение товара,
анализ поставщиков, финансовый анализ и CSV-выгрузки.

Цена продажи для оценки склада — MobaPrice.our_base (её видит покупатель),
себестоимость — Product.purchase_price, остаток — сумма Stock.quantity по городам.
"""
import csv
from datetime import datetime, timedelta
from decimal import Decimal
from io import StringIO

from sqlalchemy import and_, case, desc, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Order, OrderItem, Product, Stock, MobaPrice, User, WholesaleApplication,
    InventoryTransaction, Supplier,
)
from app.models.inventory_transaction import TransactionType
from app.models.order import OrderStatus
from app.models.wholesale_application import ApplicationStatus

ZERO = Decimal("0")


def _stock_subquery():
    """Остаток товара по всем городам: product_id -> qty."""
    return (
        select(Stock.product_id, func.sum(Stock.quantity).label("qty"))
        .group_by(Stock.product_id)
        .subquery()
    )


def _sold_subquery(since: datetime | None = None):
    """Продано (шт. и выручка) по товарам в неотменённых заказах."""
    q = (
        select(
            OrderItem.product_id,
            func.sum(OrderItem.quantity).label("sold_qty"),
            func.sum(OrderItem.quantity * OrderItem.price_at_time).label("revenue"),
        )
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.status != OrderStatus.CANCELLED)
        .group_by(OrderItem.product_id)
    )
    if since is not None:
        q = q.where(Order.created_at >= since)
    return q.subquery()


# ───────────────────────── Сводка склада ─────────────────────────


def stocked():
    """Товар, который мы держим на складе: хоть раз приходовали. Только такие считаем «заканчивающимися» —
    иначе все 11 тыс. позиций каталога Мобы с нулевым остатком попадали в «низкий остаток»."""
    return exists().where(InventoryTransaction.product_id == Product.id,
                          InventoryTransaction.type.in_([TransactionType.RECEIPT, TransactionType.ADJUSTMENT]))


async def stock_summary(db: AsyncSession) -> dict:
    st = _stock_subquery()
    qty = func.coalesce(st.c.qty, 0)
    row = (
        await db.execute(
            select(
                func.count(Product.id).label("total_parts"),
                func.coalesce(func.sum(qty), 0).label("total_stock"),
                func.coalesce(func.sum(qty * func.coalesce(MobaPrice.our_base, 0)), 0).label("retail_value"),
                func.coalesce(func.sum(qty * func.coalesce(Product.purchase_price, 0)), 0).label("cost_value"),
                func.coalesce(func.sum(case((and_(qty <= Product.critical_stock_threshold, stocked()), 1), else_=0)), 0).label("low_stock"),
            )
            .select_from(Product)
            .outerjoin(st, st.c.product_id == Product.id)
            .outerjoin(MobaPrice, MobaPrice.sku == Product.sku)
            .where(Product.is_active == 1)
        )
    ).one()
    retail, cost = Decimal(row.retail_value), Decimal(row.cost_value)
    margin = retail - cost
    return {
        "total_parts": row.total_parts,
        "total_stock": int(row.total_stock),
        "retail_value": retail,
        "cost_value": cost,
        "margin_value": margin,
        "margin_pct": float(margin / retail * 100) if retail > 0 else 0.0,
        "low_stock": int(row.low_stock),
        "total_users": (await db.execute(select(func.count(User.id)))).scalar_one(),
        "pending_orders": (
            await db.execute(select(func.count(Order.id)).where(Order.status == OrderStatus.PENDING))
        ).scalar_one(),
        "pending_wholesale": (
            await db.execute(
                select(func.count(WholesaleApplication.id)).where(
                    WholesaleApplication.status == ApplicationStatus.PENDING
                )
            )
        ).scalar_one(),
    }


# ───────────────────────── KPI продаж ─────────────────────────


async def sales_kpi(db: AsyncSession) -> dict:
    now = datetime.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    async def period(since: datetime | None) -> dict:
        q = select(func.count(Order.id), func.coalesce(func.sum(Order.total_amount), 0)).where(
            Order.status != OrderStatus.CANCELLED
        )
        if since is not None:
            q = q.where(Order.created_at >= since)
        count, revenue = (await db.execute(q)).one()
        revenue = Decimal(revenue)
        return {"count": count, "revenue": revenue, "avg_check": revenue / count if count else ZERO}

    status_rows = (await db.execute(select(Order.status, func.count(Order.id)).group_by(Order.status))).all()
    return {
        "today": await period(today_start),
        "week": await period(now - timedelta(days=7)),
        "month": await period(now - timedelta(days=30)),
        "all": await period(None),
        "status_dist": {s.name: c for s, c in status_rows},
    }


async def top_clients(db: AsyncSession, limit: int = 10) -> list:
    q = (
        select(
            User,
            func.count(Order.id).label("orders_count"),
            func.sum(Order.total_amount).label("total_spent"),
            func.avg(Order.total_amount).label("avg_check"),
            func.max(Order.created_at).label("last_order"),
        )
        .join(Order, Order.user_id == User.id)
        .where(Order.status != OrderStatus.CANCELLED)
        .group_by(User.id)
        .order_by(desc("total_spent"))
        .limit(limit)
    )
    return (await db.execute(q)).all()


async def top_products(db: AsyncSession, limit: int = 10) -> list:
    sold = _sold_subquery()
    q = (
        select(Product, sold.c.sold_qty, sold.c.revenue)
        .join(sold, sold.c.product_id == Product.id)
        .order_by(desc(sold.c.sold_qty))
        .limit(limit)
    )
    return (await db.execute(q)).all()


# ───────────────────────── ABC-анализ ─────────────────────────


async def abc_analysis(db: AsyncSession) -> dict:
    """
    A — товары, дающие первые 80% накопленной выручки, B — следующие 15%,
    C — последние 5% и все активные товары без продаж.
    """
    sold = _sold_subquery()
    st = _stock_subquery()
    rows = (
        await db.execute(
            select(Product.id, Product.sku, Product.name, func.coalesce(st.c.qty, 0).label("stock_qty"),
                   sold.c.sold_qty, sold.c.revenue)
            .join(sold, sold.c.product_id == Product.id)
            .outerjoin(st, st.c.product_id == Product.id)
            .order_by(desc(sold.c.revenue))
        )
    ).all()
    total_revenue = sum((Decimal(r.revenue) for r in rows), ZERO)
    total_active = (await db.execute(select(func.count(Product.id)).where(Product.is_active == 1))).scalar_one()

    groups = {"A": [], "B": [], "C": []}
    cum = ZERO
    for r in rows:
        rev = Decimal(r.revenue)
        cum += rev
        pct = float(cum / total_revenue * 100) if total_revenue else 100.0
        g = "A" if pct <= 80 else "B" if pct <= 95 else "C"
        groups[g].append({
            "id": r.id, "sku": r.sku, "name": r.name, "stock_qty": int(r.stock_qty),
            "sold_qty": int(r.sold_qty), "revenue": rev,
            "share": float(rev / total_revenue * 100) if total_revenue else 0.0,
            "cumulative": pct, "group": g,
        })

    unsold = max(0, total_active - len(rows))
    # Неликвид: товары без продаж, но с наибольшим остатком — кандидаты на акции
    sold_ids = select(OrderItem.product_id).join(Order).where(Order.status != OrderStatus.CANCELLED)
    dead_stock = (
        await db.execute(
            select(Product.id, Product.sku, Product.name, st.c.qty)
            .join(st, st.c.product_id == Product.id)
            .where(Product.is_active == 1, st.c.qty > 0, Product.id.not_in(sold_ids))
            .order_by(desc(st.c.qty))
            .limit(10)
        )
    ).all()

    revenue = {g: sum((i["revenue"] for i in items), ZERO) for g, items in groups.items()}
    return {
        "total_revenue": total_revenue,
        "groups": groups,
        "counts": {"A": len(groups["A"]), "B": len(groups["B"]), "C": len(groups["C"]) + unsold},
        "unsold": unsold,
        "revenue": revenue,
        "pct": {g: float(v / total_revenue * 100) if total_revenue else 0.0 for g, v in revenue.items()},
        "dead_stock": dead_stock,
    }


# ───────────────────────── Прогноз закупок ─────────────────────────

URGENCY_LABELS = {
    "EMPTY": "Закончился",
    "HIGH": "< 7 дней",
    "MEDIUM": "< 14 дней",
    "OK": "В норме",
}


async def procurement_forecast(db: AsyncSession, days_window: int = 30, only_urgent: bool = True) -> list[dict]:
    """
    Темп расхода (продажи за days_window дней), запас в днях и рекомендуемый дозаказ
    до 30-дневного запаса. Логика повторяет db.get_procurement_forecast бота.
    """
    sold = _sold_subquery(datetime.now() - timedelta(days=days_window))
    st = _stock_subquery()
    rows = (
        await db.execute(
            select(Product.id, Product.sku, Product.name, Product.critical_stock_threshold,
                   Product.purchase_price, Supplier.name.label("supplier"),
                   func.coalesce(st.c.qty, 0).label("stock_qty"),
                   func.coalesce(sold.c.sold_qty, 0).label("sold"))
            .outerjoin(st, st.c.product_id == Product.id)
            .outerjoin(sold, sold.c.product_id == Product.id)
            .outerjoin(Supplier, Supplier.id == Product.supplier_id)
            .where(Product.is_active == 1)
        )
    ).all()

    result = []
    for r in rows:
        stock, threshold = int(r.stock_qty), r.critical_stock_threshold
        burn = int(r.sold) / days_window
        if burn > 0:
            days_left = round(stock / burn, 1)
            recommended = max(max(0, int(burn * 30) - stock), threshold * 2 - stock if stock <= threshold else 0)
        else:
            days_left = None if stock > 0 else 0.0
            recommended = threshold * 2 if stock == 0 else 0

        if stock == 0:
            urgency = "EMPTY"
        elif (days_left is not None and days_left <= 7) or stock <= threshold:
            urgency = "HIGH"
        elif days_left is not None and days_left <= 14:
            urgency = "MEDIUM"
        else:
            urgency = "OK"
        if only_urgent and urgency == "OK":
            continue
        result.append({
            "id": r.id, "sku": r.sku, "name": r.name, "stock_qty": stock, "threshold": threshold,
            "daily_burn": round(burn, 2), "days_left": days_left, "recommended": recommended,
            "supplier": r.supplier, "purchase_price": r.purchase_price,
            "urgency": urgency, "urgency_label": URGENCY_LABELS[urgency],
        })

    weight = {"EMPTY": 0, "HIGH": 1, "MEDIUM": 2, "OK": 3}
    result.sort(key=lambda x: (weight[x["urgency"]], x["days_left"] if x["days_left"] is not None else 9999))
    return result


# ───────────────────────── Склад: движение ─────────────────────────


async def movement_report(db: AsyncSession, days: int = 30) -> list:
    """Приход/расход по товарам за период (по журналу inventory_transactions)."""
    since = datetime.now() - timedelta(days=days)
    q = (
        select(
            Product.id, Product.sku, Product.name,
            func.sum(case((InventoryTransaction.quantity > 0, InventoryTransaction.quantity), else_=0)).label("inflow"),
            func.sum(case((InventoryTransaction.quantity < 0, -InventoryTransaction.quantity), else_=0)).label("outflow"),
            func.max(InventoryTransaction.created_at).label("last_date"),
        )
        .join(Product, Product.id == InventoryTransaction.product_id)
        .where(InventoryTransaction.created_at >= since)
        .group_by(Product.id)
        .order_by(desc("last_date"))
    )
    return (await db.execute(q)).all()


async def supplier_analysis(db: AsyncSession) -> list:
    """Поставки по поставщикам: число приходов, штук, средняя партия, сумма закупки, последняя поставка."""
    T = InventoryTransaction
    q = (
        select(
            Supplier,
            func.count(T.id).label("deliveries"),
            func.coalesce(func.sum(T.quantity), 0).label("total_received"),
            func.avg(T.quantity).label("avg_batch"),
            func.coalesce(func.sum(T.quantity * func.coalesce(T.unit_cost, 0)), 0).label("total_cost"),
            func.max(T.created_at).label("last_delivery"),
        )
        .outerjoin(T, (T.supplier_id == Supplier.id) & (T.type == TransactionType.RECEIPT))
        .group_by(Supplier.id)
        .order_by(desc("total_received"), Supplier.name)
    )
    return (await db.execute(q)).all()


# ───────────────────────── Финансы ─────────────────────────


async def finance_summary(db: AsyncSession) -> dict:
    completed = (
        await db.execute(
            select(
                func.coalesce(func.sum(Order.total_amount), 0),
                func.coalesce(func.sum(Order.profit), 0),
                func.count(Order.id),
            ).where(Order.status == OrderStatus.COMPLETED)
        )
    ).one()
    avg_margin = (
        await db.execute(
            select(func.avg((MobaPrice.our_base - Product.purchase_price) * 100 / MobaPrice.our_base))
            .select_from(Product)
            .join(MobaPrice, MobaPrice.sku == Product.sku)
            .where(Product.is_active == 1, Product.purchase_price.isnot(None), MobaPrice.our_base > 0)
        )
    ).scalar()
    avg_cost = (await db.execute(select(func.avg(Product.purchase_price)))).scalar()
    revenue, profit = Decimal(completed[0]), Decimal(completed[1])
    return {
        "revenue": revenue,
        "profit": profit,
        "order_count": completed[2],
        "profit_pct": float(profit / revenue * 100) if revenue > 0 else 0.0,
        "avg_margin": float(avg_margin or 0),
        "avg_cost": Decimal(avg_cost or 0).quantize(Decimal("0.01")),
    }


async def top_margin_products(db: AsyncSession, limit: int = 10) -> list:
    margin = ((MobaPrice.our_base - Product.purchase_price) * 100 / MobaPrice.our_base).label("margin")
    q = (
        select(Product.id, Product.sku, Product.name, Product.purchase_price, MobaPrice.our_base, margin)
        .join(MobaPrice, MobaPrice.sku == Product.sku)
        .where(Product.is_active == 1, Product.purchase_price.isnot(None), MobaPrice.our_base > 0)
        .order_by(desc("margin"))
        .limit(limit)
    )
    return (await db.execute(q)).all()


# ───────────────────────── CSV ─────────────────────────


def _csv(rows: list[list], header: list[str]) -> str:
    """CSV для Excel: разделитель «;», BOM добавляет вызывающий код."""
    buf = StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(header)
    # Имя покупателя вида «=HYPERLINK(…)» Excel выполнил бы как формулу — экранируем апострофом
    w.writerows([["'" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@") else v for v in r] for r in rows])
    return buf.getvalue()


async def export_stock_csv(db: AsyncSession) -> str:
    st = _stock_subquery()
    rows = (
        await db.execute(
            select(Product.id, Product.sku, Product.name, Supplier.name, Product.purchase_price,
                   MobaPrice.our_base, MobaPrice.our_opt, func.coalesce(st.c.qty, 0), Product.critical_stock_threshold,
                   Product.is_active)
            .outerjoin(st, st.c.product_id == Product.id)
            .outerjoin(MobaPrice, MobaPrice.sku == Product.sku)
            .outerjoin(Supplier, Supplier.id == Product.supplier_id)
            .order_by(Product.id)
        )
    ).all()
    return _csv(
        [list(r) for r in rows],
        ["ID", "Артикул", "Название", "Поставщик", "Себестоимость", "Цена (база)", "Цена (опт)",
         "Остаток (шт)", "Порог алерта", "Активен"],
    )


async def export_movements_csv(db: AsyncSession, days: int = 60) -> str:
    T = InventoryTransaction
    since = datetime.now() - timedelta(days=days)
    rows = (
        await db.execute(
            select(T.id, T.created_at, T.type, Product.sku, Product.name, T.quantity, T.unit_cost,
                   Supplier.name, T.notes, User.username)
            .join(Product, Product.id == T.product_id)
            .outerjoin(Supplier, Supplier.id == T.supplier_id)
            .outerjoin(User, User.id == T.user_id)
            .where(T.created_at >= since)
            .order_by(T.created_at.desc())
        )
    ).all()
    return _csv(
        [[r[0], r[1], r[2].name, *r[3:]] for r in rows],
        ["ID", "Дата", "Тип", "Артикул", "Товар", "Кол-во", "Цена закупки", "Поставщик", "Примечание", "Сотрудник"],
    )


async def export_orders_csv(db: AsyncSession) -> str:
    rows = (
        await db.execute(
            select(Order.id, Order.created_at, User.username, Order.customer_name, Order.customer_phone,
                   Order.status, Order.order_type, Order.total_amount, Order.profit)
            .outerjoin(User, User.id == Order.user_id)
            .order_by(Order.id)
        )
    ).all()
    return _csv(
        [[r[0], r[1], r[2] or "", r[3], r[4], r[5].name, r[6].name, r[7], r[8]] for r in rows],
        ["ID", "Создан", "Пользователь", "Клиент", "Телефон", "Статус", "Тип", "Сумма", "Прибыль"],
    )


async def export_analytics_csv(db: AsyncSession) -> str:
    """Комплексная аналитика: KPI, топ клиентов, ABC и прогноз закупок — как в боте."""
    kpi = await sales_kpi(db)
    clients = await top_clients(db, limit=30)
    abc = await abc_analysis(db)
    forecast = await procurement_forecast(db)

    buf = StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["=== KPI ПРОДАЖ ==="])
    w.writerow(["Период", "Заказов", "Выручка", "Средний чек"])
    for key, label in (("today", "Сегодня"), ("week", "7 дней"), ("month", "30 дней"), ("all", "Всё время")):
        p = kpi[key]
        w.writerow([label, p["count"], p["revenue"], round(p["avg_check"], 2)])
    w.writerow([])
    w.writerow(["=== ТОП КЛИЕНТОВ ==="])
    w.writerow(["Пользователь", "Роль", "Заказов", "Выручка", "Средний чек", "Последний заказ"])
    for c in clients:
        w.writerow([c.User.username, c.User.role.name, c.orders_count, c.total_spent, round(c.avg_check, 2), c.last_order])
    w.writerow([])
    w.writerow(["=== ABC-АНАЛИЗ ==="])
    w.writerow(["Артикул", "Товар", "Группа", "Остаток", "Продано", "Выручка", "Доля %", "Накопленная %"])
    for g in ("A", "B", "C"):
        for i in abc["groups"][g]:
            w.writerow([i["sku"], i["name"], g, i["stock_qty"], i["sold_qty"], i["revenue"],
                        round(i["share"], 2), round(i["cumulative"], 2)])
    w.writerow([f"Без продаж (группа C): {abc['unsold']} поз."])
    w.writerow([])
    w.writerow(["=== ПРОГНОЗ ЗАКУПОК ==="])
    w.writerow(["Артикул", "Товар", "Срочность", "Остаток", "Расход/день", "Хватит на (дней)", "Дозаказать", "Поставщик"])
    for f in forecast:
        w.writerow([f["sku"], f["name"], f["urgency_label"], f["stock_qty"], f["daily_burn"],
                    f["days_left"] if f["days_left"] is not None else "—", f["recommended"], f["supplier"] or ""])
    return buf.getvalue()
