"""
Админ-панель Parts.Site.

Доступ только у администратора (роль ADMIN или флаг is_admin).
Остальные роли — покупатели: розничный (RETAIL) и оптовый (WHOLESALE).
"""
import re
import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import select, func, or_, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from app.database import get_db
from app.models import (
    User, SystemConfig, Order, OrderItem, Product, Category, City, Stock, MobaPrice,
    WholesaleApplication, Supplier, Mailing, InventoryTransaction,
)
from app.models.inventory_transaction import TransactionType
from app.models.order import OrderStatus
from app.models.user import UserRole
from app.models.wholesale_application import ApplicationStatus
from app.models.chat import ChatMessage, ChatStatus, ChatThread
from app.models.user_cart import UserCart
from app.services import analytics, accounts, cart, catalog_tree, chat, order_service, search, telegram
from app.services.pricing import buyer_prices, reference_prices, assign_prices
from app.services.pricing import invalidate_config_cache, recalculate_all, round_money
from app.templating import templates

router = APIRouter(prefix="/admin", tags=["admin"])

PAGE_SIZE = 50

# Разделы навигации: (url, название)
NAV = [
    ("Обзор", [
        ("/admin/dashboard", "Дашборд"),
    ]),
    ("Продажи", [
        ("/admin/chats", "Обращения"),
        ("/admin/orders", "Заказы"),
        ("/admin/analytics", "Продажи и ABC"),
        ("/admin/finance", "Финансы"),
        ("/admin/wholesale", "Заявки на опт"),
    ]),
    ("Склад", [
        ("/admin/products", "Товары"),
        ("/admin/products/receive", "Приход товара"),
        ("/admin/products/writeoff", "Списание"),
        ("/admin/inventory", "Движение товара"),
        ("/admin/alerts", "Низкие остатки"),
        ("/admin/forecast", "Прогноз закупок"),
        ("/admin/suppliers", "Поставщики"),
    ]),
    ("Администрирование", [
        ("/admin/users", "Пользователи"),
        ("/admin/mailing", "Рассылки"),
        ("/admin/settings", "Настройки цен"),
        ("/admin/integration-1c", "Интеграция с 1С"),
        ("/admin/export", "Экспорт CSV"),
    ]),
]


def require():
    """Зависимость: пользователь авторизован и является администратором."""
    async def dependency(request: Request, db: AsyncSession = Depends(get_db)) -> User:
        user_id = request.session.get("user_id")
        if not user_id:
            raise HTTPException(status_code=303, headers={"Location": "/auth/login"})
        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
        if (not user or not user.can_sign_in
                or request.session.get("pwv") != accounts.session_fingerprint(user)):
            raise HTTPException(status_code=303, headers={"Location": "/auth/login"})
        if not user.is_superuser:
            raise HTTPException(status_code=403, detail="Недостаточно прав для этого раздела")
        return user
    return dependency


def render(request: Request, name: str, admin: User, **context) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name=name,
        context={"user": admin, "nav": NAV, "path": request.url.path,
                 "msg": request.query_params.get("msg"), "error": request.query_params.get("error"),
                 **context},
    )


def back(url: str, msg: str | None = None, error: str | None = None) -> RedirectResponse:
    if msg:
        url += ("&" if "?" in url else "?") + "msg=" + quote(msg)
    if error:
        url += ("&" if "?" in url else "?") + "error=" + quote(error)
    return RedirectResponse(url=url, status_code=303)


def _decimal(value: str | None, field: str, allow_empty: bool = True) -> Decimal | None:
    if value is None or not value.strip():
        if allow_empty:
            return None
        raise ValueError(f"Поле «{field}» обязательно")
    try:
        d = Decimal(value.replace(",", ".").strip())
    except InvalidOperation:
        raise ValueError(f"Поле «{field}»: введите число")
    if not d.is_finite():
        raise ValueError(f"Поле «{field}»: введите конечное число")
    if d < 0:
        raise ValueError(f"Поле «{field}» не может быть отрицательным")
    return d.quantize(Decimal("0.01"))


async def _refresh_product_prices(db, product):
    mp = (await db.execute(select(MobaPrice).where(MobaPrice.sku == product.sku))).scalar_one_or_none()
    if mp and mp.price > 0:
        assign_prices(mp, product, reference_prices(mp.price, purchase_price=product.purchase_price))


async def _check_city(db: AsyncSession, city_id: int) -> bool:
    return (await db.execute(select(City.id).where(City.id == city_id))).first() is not None


async def _supplier_id(db: AsyncSession, raw: str | None) -> int | None:
    """id поставщика из формы; пусто — без поставщика, несуществующий — ошибка."""
    if not raw:
        return None
    try:
        sid = int(raw)
    except ValueError:
        raise ValueError("Неизвестный поставщик")
    if not (await db.execute(select(Supplier.id).where(Supplier.id == sid))).first():
        raise ValueError("Неизвестный поставщик")
    return sid


def _pages(total: int, page: int) -> tuple[int, int]:
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    return pages, min(max(page, 1), pages)


@router.get("")
@router.get("/")
async def admin_root():
    return RedirectResponse(url="/admin/dashboard", status_code=303)


# ───────────────────────── Дашборд ─────────────────────────


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, db: AsyncSession = Depends(get_db), admin: User = Depends(require())):
    return render(
        request, "admin_dashboard.html", admin,
        stock=await analytics.stock_summary(db),
        kpi=await analytics.sales_kpi(db),
        top_clients=await analytics.top_clients(db),
        top_products=await analytics.top_products(db),
    )


# ───────────────────────── Заказы ─────────────────────────


@router.get("/orders", response_class=HTMLResponse)
async def admin_orders(request: Request, status: str = "all", page: int = 1,
                       db: AsyncSession = Depends(get_db), admin: User = Depends(require())):
    query = select(Order)
    count_q = select(func.count(Order.id))
    if status in OrderStatus.__members__:
        query = query.where(Order.status == OrderStatus[status])
        count_q = count_q.where(Order.status == OrderStatus[status])
    else:
        status = "all"
    total = (await db.execute(count_q)).scalar_one()
    pages, page = _pages(total, page)
    orders = (
        await db.execute(
            query.options(joinedload(Order.user), joinedload(Order.city))
            .order_by(Order.created_at.desc())
            .offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)
        )
    ).scalars().all()
    counts = dict((await db.execute(select(Order.status, func.count(Order.id)).group_by(Order.status))).all())
    return render(request, "admin_orders.html", admin, orders=orders, OrderStatus=OrderStatus,
                  status=status, page=page, pages=pages, total=total,
                  counts={s.name: c for s, c in counts.items()})


@router.get("/orders/{order_id:int}", response_class=HTMLResponse)
async def admin_order_detail(order_id: int, request: Request, db: AsyncSession = Depends(get_db),
                             admin: User = Depends(require())):
    order = (
        await db.execute(
            select(Order)
            .options(joinedload(Order.user), joinedload(Order.city),
                     selectinload(Order.items).selectinload(OrderItem.product))
            .where(Order.id == order_id)
        )
    ).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    return render(request, "admin_order_detail.html", admin, order=order, OrderStatus=OrderStatus)


@router.post("/orders/{order_id}/note")
async def order_note(order_id: int, note: str = Form(""), db: AsyncSession = Depends(get_db), admin: User = Depends(require())):
    order = await db.get(Order, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    order.manager_note = note.strip()[:1000] or None
    await db.commit()
    return back(f"/admin/orders/{order_id}", msg="Заметка сохранена")


@router.get("/orders/new", response_class=HTMLResponse)
async def order_new(request: Request, thread: int | None = None, user: int | None = None,
                    db: AsyncSession = Depends(get_db), admin: User = Depends(require())):
    """Заказ за покупателя — из обращения или по звонку. Покупатель и его корзина подставляются."""
    customer, chat_thread, rows = None, None, []
    if thread:
        chat_thread = await db.get(ChatThread, thread)
        if chat_thread and chat_thread.user_id:
            user = chat_thread.user_id
    if user:
        customer = await db.get(User, user)
    if customer:
        for qty, mp in (await db.execute(
            select(UserCart.quantity, MobaPrice).join(Product, Product.id == UserCart.product_id)
            .join(MobaPrice, MobaPrice.sku == Product.sku).where(UserCart.user_id == customer.id)
        )).all():
            rows.append({"sku": mp.sku, "name": mp.name, "qty": qty,
                         "price": float(mp.our_base) if mp.our_base is not None else None,
                         "opt": float(mp.our_opt) if mp.our_opt else None})
    last = None
    if customer:
        last = (await db.execute(select(Order).where(Order.user_id == customer.id).order_by(Order.id.desc()).limit(1))).scalar_one_or_none()
    prefill = {
        "user_id": customer.id if customer else "",
        "user_label": (customer.full_name or customer.username) if customer else "",
        "name": (last.customer_name if last else None) or (customer.full_name if customer else None) or (chat_thread.name if chat_thread else "") or "",
        "phone": (last.customer_phone if last else None) or (customer.phone if customer else None) or (chat_thread.phone if chat_thread else "") or "",
        "wholesale": bool(customer and customer.is_wholesale),
        "thread": chat_thread.id if chat_thread else "",
    }
    cities = (await db.execute(select(City).where(City.is_active.is_(True)).order_by(City.id))).scalars().all()
    return render(request, "admin_order_new.html", admin, prefill=prefill, rows=rows, cities=cities)


@router.get("/api/users")
async def admin_find_users(q: str = "", db: AsyncSession = Depends(get_db), admin: User = Depends(require())):
    """Поиск покупателя для заказа: логин, имя, телефон."""
    q = q.strip()
    if len(q) < 2:
        return JSONResponse({"items": []})
    like = f"%{q}%"
    users = (await db.execute(select(User).where(or_(User.username.ilike(like), User.full_name.ilike(like), User.phone.ilike(like)))
                              .order_by(User.id.desc()).limit(8))).scalars().all()
    return JSONResponse({"items": [{"id": u.id, "label": u.full_name or u.username, "username": u.username, "phone": u.phone or "",
                                    "wholesale": u.is_wholesale} for u in users]})


@router.post("/orders/new")
async def order_create(request: Request, db: AsyncSession = Depends(get_db), admin: User = Depends(require())):
    form = await request.form()
    url = "/admin/orders/new" + (f"?thread={form.get('thread')}" if form.get("thread") else
                                 f"?user={form.get('user_id')}" if form.get("user_id") else "")
    name, phone = (form.get("customer_name") or "").strip()[:200], (form.get("customer_phone") or "").strip()[:50]
    if not name or sum(ch.isdigit() for ch in phone) < 5:
        return back(url, error="Укажите имя и телефон покупателя")
    customer = await db.get(User, int(form["user_id"])) if (form.get("user_id") or "").isdigit() else None
    wholesale = customer.is_wholesale if customer else form.get("price_type") == "opt"
    try:
        city_id = int(form.get("city_id") or 0)
    except ValueError:
        city_id = 0
    if not await _check_city(db, city_id):
        return back(url, error="Выберите склад")
    wanted: dict[str, int] = {}
    for sku, qty in zip(form.getlist("sku"), form.getlist("qty")):
        sku = sku.strip()
        if not sku:
            continue
        try:
            n = int(qty)
        except ValueError:
            return back(url, error=f"Количество для {sku}: введите число")
        if n > 0:
            wanted[sku] = wanted.get(sku, 0) + min(n, 999)
    if not wanted:
        return back(url, error="Добавьте товары")
    prices = {mp.sku: mp for mp in (await db.execute(select(MobaPrice).where(MobaPrice.sku.in_(list(wanted))))).scalars()}
    items = []
    for sku, qty in wanted.items():
        mp = prices.get(sku)
        price = buyer_prices(mp, wholesale, authenticated=bool(customer), price_type=customer.effective_price_type if customer else None, pickup=bool(customer) and not (form.get("address") or "").strip())["price"] if mp else None
        if price is None:
            return back(url, error=f"У товара {sku} нет цены — задайте её в карточке товара")
        items.append({"sku": sku, "qty": qty, "price": price})
    try:
        order = await order_service.create_order(
            db=db, user_id=customer.id if customer else None, city_id=city_id, cart_items=items,
            customer_name=name, customer_phone=phone, address=(form.get("address") or "").strip()[:500] or None,
            wholesale=wholesale, comment=(form.get("comment") or "").strip()[:1000] or None, source="admin",
        )
        order.manager_note = (form.get("manager_note") or "").strip()[:1000] or None
        if customer and form.get("clear_cart"):
            await cart.user_clear(db, customer.id, commit=False)
        thread = await db.get(ChatThread, int(form["thread"])) if (form.get("thread") or "").isdigit() else None
        if thread:
            await chat.add_message(db, thread, "staff", f"Оформил для вас заказ № {order.id} на {_rub(order.total_amount)}. "
                                                        "Менеджер свяжется для подтверждения.", author_id=admin.id)
        await db.commit()
    except order_service.OrderError as e:
        return back(url, error=str(e))
    telegram.fire(telegram.notify_new_order(order.id))
    return back(f"/admin/orders/{order.id}", msg=f"Заказ № {order.id} оформлен")


def _rub(value) -> str:
    return telegram.money(value).replace("\u00a0", " ")


@router.post("/orders/{order_id}/status")
async def update_order_status(order_id: int, request: Request, status: str = Form(...),
                              db: AsyncSession = Depends(get_db), admin: User = Depends(require())):
    referer = request.headers.get("referer") or ""
    target = f"/admin/orders/{order_id}" if f"/admin/orders/{order_id}" in referer else "/admin/orders"
    if status not in OrderStatus.__members__:
        return back(target, error="Неизвестный статус")
    current = await db.get(Order, order_id)
    if current and current.status.name == status:
        return back(target, msg="Статус не изменился")
    try:
        await order_service.change_order_status(db, order_id, OrderStatus[status], admin.id)
        await db.commit()
    except order_service.OrderError as e:
        return back(target, error=str(e))
    telegram.fire(telegram.notify_order_status(order_id))
    return back(target, msg=f"Статус заказа #{order_id} изменён")


# ───────────────────────── Товары ─────────────────────────


@router.get("/products", response_class=HTMLResponse)
async def admin_products(request: Request, q: str = "", page: int = 1, show: str = "active",
                         db: AsyncSession = Depends(get_db), admin: User = Depends(require())):
    st = (select(Stock.product_id, func.sum(Stock.quantity).label("qty")).group_by(Stock.product_id).subquery())
    conditions = []
    if q.strip():
        # Тот же поиск по ключевым словам, что и на витрине, плюс подстрока артикула
        conditions.append(or_(Product.sku.ilike(f"%{q.strip()}%"), and_(*search.conditions(q))))
    if show == "hidden":
        conditions.append(Product.is_active == 0)
    elif show == "low":
        conditions.extend([func.coalesce(st.c.qty, 0) <= Product.critical_stock_threshold, analytics.stocked()])
    else:
        show = "active"
        conditions.append(Product.is_active == 1)

    base = (
        select(Product, func.coalesce(st.c.qty, 0).label("qty"), MobaPrice.our_base, MobaPrice.our_opt)
        .outerjoin(st, st.c.product_id == Product.id)
        .outerjoin(MobaPrice, MobaPrice.sku == Product.sku)
        .where(*conditions)
    )
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    pages, page = _pages(total, page)
    order = search.order_by(q) if q.strip() else [Product.id]
    rows = (await db.execute(base.order_by(*order).offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE))).all()
    return render(request, "admin_products.html", admin, rows=rows, q=q, show=show,
                  page=page, pages=pages, total=total)


@router.get("/api/suggest")
async def admin_suggest(q: str = "", db: AsyncSession = Depends(get_db), admin: User = Depends(require())):
    """Подсказки для полей «Артикул» в приходе/списании: товары (включая скрытые) с общим остатком."""
    if len(q.strip()) < 2:
        return JSONResponse({"items": []})
    st = select(Stock.product_id, func.sum(Stock.quantity).label("qty")).group_by(Stock.product_id).subquery()
    rows = (
        await db.execute(
            select(Product.sku, Product.name, func.coalesce(st.c.qty, 0), MobaPrice.our_base, MobaPrice.our_opt)
            .join(MobaPrice, MobaPrice.sku == Product.sku)
            .outerjoin(st, st.c.product_id == Product.id)
            .where(or_(Product.sku.ilike(f"{q.strip()}%"), and_(*search.conditions(q))))
            .order_by(*search.order_by(q))
            .limit(10)
        )
    ).all()
    return JSONResponse({"items": [
        {"sku": sku, "name": name, "stock": int(qty), "url": "#",
         "price": float(base) if base is not None else None, "opt": float(opt) if opt else None}
        for sku, name, qty, base, opt in rows
    ]})


async def _product_form_context(db: AsyncSession) -> dict:
    return {
        "categories": (await db.execute(select(Category).order_by(Category.name))).scalars().all(),
        "suppliers": (await db.execute(select(Supplier).order_by(Supplier.name))).scalars().all(),
        "cities": (await db.execute(select(City).where(City.is_active.is_(True)).order_by(City.id))).scalars().all(),
    }


@router.get("/products/new", response_class=HTMLResponse)
async def product_new(request: Request, db: AsyncSession = Depends(get_db),
                      admin: User = Depends(require())):
    return render(request, "admin_product_form.html", admin, product=None, mp=None, stocks={},
                  **await _product_form_context(db))


@router.post("/products/new")
async def product_create(request: Request, db: AsyncSession = Depends(get_db),
                         admin: User = Depends(require())):
    form = await request.form()
    sku = (form.get("sku") or "").strip()
    name = (form.get("name") or "").strip()
    try:
        if not sku or not name:
            raise ValueError("Артикул и название обязательны")
        if (await db.execute(select(Product.id).where(Product.sku == sku))).first():
            raise ValueError(f"Товар с артикулом {sku} уже существует")
        opt = round_money(_decimal(form.get("price_opt"), "ОПТ / Moba", allow_empty=False))
        if opt <= 0:
            raise ValueError("Цена должна быть больше нуля")
        cost = _decimal(form.get("purchase_price"), "Себестоимость")
        calculated = reference_prices(opt, purchase_price=cost)
        base, card, opt = calculated["base"], calculated["card"], calculated["opt"]
        threshold = int(form.get("critical_stock_threshold") or 5)
        if threshold < 0:
            raise ValueError("Порог не может быть отрицательным")
        category_id = int(form.get("category_id"))
        if not (await db.execute(select(Category.id).where(Category.id == category_id))).first():
            raise ValueError("Выберите категорию")
        supplier_id = await _supplier_id(db, form.get("supplier_id"))
    except (ValueError, TypeError) as e:
        return back("/admin/products/new", error=str(e) if str(e) else "Проверьте поля формы")

    slug = re.sub(r"[^a-z0-9]+", "-", sku.lower()).strip("-") or sku
    product = Product(
        sku=sku, name=name, slug=slug, category_id=category_id,
        description=(form.get("description") or "").strip() or None,
        price_base=base, price_card=card, price_opt=opt, price_super_opt=calculated["super_opt"], purchase_price=cost,
        critical_stock_threshold=threshold, is_active=1, supplier_id=supplier_id,
    )
    db.add(product)
    # Витрина читает цены из moba_prices — создаём (или обновляем оставшуюся от парсера) запись с нашими ценами
    mp = (await db.execute(select(MobaPrice).where(MobaPrice.sku == sku))).scalar_one_or_none()
    if mp:
        mp.name, mp.price, mp.our_base, mp.our_card, mp.our_opt, mp.url = name, opt, base, card, opt, None
        mp.our_super_opt = calculated["super_opt"]
    else:
        db.add(MobaPrice(sku=sku, name=name, price=opt, our_base=base, our_card=card, our_opt=opt, our_super_opt=calculated["super_opt"]))
    await db.flush()
    for city in (await db.execute(select(City))).scalars().all():
        db.add(Stock(product_id=product.id, city_id=city.id, quantity=0))
    await db.commit()
    catalog_tree.invalidate()
    return back(f"/admin/products/{product.id}", msg="Товар создан. Оприходуйте остаток через «Приход товара».")


@router.get("/products/receive", response_class=HTMLResponse)
async def admin_receive_product(request: Request, db: AsyncSession = Depends(get_db),
                                admin: User = Depends(require())):
    ctx = await _product_form_context(db)
    return render(request, "admin_receive.html", admin, article=request.query_params.get("article"),
                  cities=ctx["cities"], suppliers=ctx["suppliers"])


@router.post("/products/receive")
async def admin_receive_product_post(
    article: str = Form(...), quantity: int = Form(...), city_id: int = Form(...),
    purchase_price: str = Form(""), supplier_id: str = Form(""), notes: str = Form(""),
    db: AsyncSession = Depends(get_db), admin: User = Depends(require()),
):
    product = (await db.execute(select(Product).where(Product.sku == article.strip()))).scalar_one_or_none()
    if not product:
        return back(f"/admin/products/receive?article={quote(article)}", error="Товар с таким артикулом не найден")
    if quantity <= 0:
        return back(f"/admin/products/receive?article={quote(article)}", error="Количество должно быть больше нуля")
    if quantity > 100_000:
        return back(f"/admin/products/receive?article={quote(article)}", error="Слишком большое количество")
    try:
        cost = _decimal(purchase_price, "Закупочная цена")
        supplier = await _supplier_id(db, supplier_id)
        if not await _check_city(db, city_id):
            raise ValueError("Выберите склад")
    except ValueError as e:
        return back(f"/admin/products/receive?article={quote(article)}", error=str(e))

    if cost is not None:
        product.purchase_price = cost
        await _refresh_product_prices(db, product)
    if supplier:
        product.supplier_id = supplier
    stock = (
        await db.execute(select(Stock).where(Stock.product_id == product.id, Stock.city_id == city_id).with_for_update())
    ).scalar_one_or_none()
    if stock:
        stock.quantity += quantity
    else:
        db.add(Stock(product_id=product.id, city_id=city_id, quantity=quantity))
    db.add(InventoryTransaction(
        product_id=product.id, user_id=admin.id, quantity=quantity, type=TransactionType.RECEIPT,
        to_city_id=city_id, supplier_id=supplier, unit_cost=cost, notes=notes.strip() or None,
    ))
    await db.commit()
    return back("/admin/inventory", msg=f"Оприходовано {quantity} шт. «{product.name}»")


def _read_rows(filename: str, data: bytes) -> list[list]:
    """Строки таблицы из .xlsx или .csv (разделитель «;» или «,», кодировка UTF-8 или Windows-1251)."""
    if filename.lower().endswith(".xlsx"):
        import io
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        return [list(r) for r in wb.worksheets[0].iter_rows(values_only=True)]
    import csv
    import io
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1251")
    dialect = csv.Sniffer().sniff(text[:2000], delimiters=";,\t")
    return list(csv.reader(io.StringIO(text), dialect))


@router.post("/products/receive/upload", response_class=HTMLResponse)
async def admin_receive_upload(request: Request, db: AsyncSession = Depends(get_db), admin: User = Depends(require())):
    """Приход поставки файлом: Артикул / Количество / Цена (закупка). Всё одной транзакцией."""
    form = await request.form()
    upload = form.get("file")
    ctx = await _product_form_context(db)
    errors: list[str] = []

    def done(ok=0, units=0):
        return render(request, "admin_receive.html", admin, article=None, cities=ctx["cities"],
                      suppliers=ctx["suppliers"], report={"ok": ok, "units": units, "errors": errors})

    try:
        city_id = int(form.get("city_id") or 0)
        supplier = await _supplier_id(db, form.get("supplier_id"))
        if not await _check_city(db, city_id):
            raise ValueError("Выберите склад")
        if not upload or not getattr(upload, "filename", ""):
            raise ValueError("Выберите файл")
        data = await upload.read()
        if len(data) > 10 * 1024 * 1024:
            raise ValueError("Файл больше 10 МБ")
        rows = [r for r in _read_rows(upload.filename, data) if any(c not in (None, "") for c in r)]
    except Exception as e:  # битый файл, неверный формат
        errors.append(str(e) or "Не удалось прочитать файл")
        return done()
    if not rows:
        errors.append("Файл пустой")
        return done()

    header = [str(c or "").strip().lower() for c in rows[0]]
    find = lambda *keys: next((i for i, h in enumerate(header) if any(k in h for k in keys)), None)
    i_sku, i_qty, i_cost = find("артикул", "sku", "код"), find("кол", "qty", "шт"), find("закуп", "цен", "price", "cost")
    if i_sku is None or i_qty is None:
        errors.append("Не нашли колонки «Артикул» и «Количество» в первой строке")
        return done()

    ok = units = 0
    for n, r in enumerate(rows[1:], start=2):
        sku = str(r[i_sku] or "").strip() if i_sku < len(r) else ""
        if not sku:
            continue
        try:
            qty = int(float(str(r[i_qty]).replace(",", ".")))
            cost = _decimal(str(r[i_cost]), "Цена") if i_cost is not None and i_cost < len(r) and r[i_cost] not in (None, "") else None
        except (TypeError, ValueError):
            errors.append(f"Строка {n} ({sku}): количество или цена — не число")
            continue
        if qty <= 0 or qty > 100_000:
            errors.append(f"Строка {n} ({sku}): количество {qty}")
            continue
        product = (await db.execute(select(Product).where(Product.sku == sku))).scalar_one_or_none()
        if not product:
            errors.append(f"Строка {n}: артикул {sku} не найден")
            continue
        if cost is not None:
            product.purchase_price = cost
            await _refresh_product_prices(db, product)
        if supplier:
            product.supplier_id = supplier
        stock = (await db.execute(select(Stock).where(Stock.product_id == product.id, Stock.city_id == city_id)
                                  .with_for_update())).scalar_one_or_none()
        if stock:
            stock.quantity += qty
        else:
            db.add(Stock(product_id=product.id, city_id=city_id, quantity=qty))
        db.add(InventoryTransaction(product_id=product.id, user_id=admin.id, quantity=qty, type=TransactionType.RECEIPT,
                                    to_city_id=city_id, supplier_id=supplier, unit_cost=cost,
                                    notes=f"Поставка файлом {upload.filename[:80]}"))
        ok += 1
        units += qty
    await db.commit()
    return done(ok, units)


@router.get("/products/writeoff", response_class=HTMLResponse)
async def admin_writeoff(request: Request, db: AsyncSession = Depends(get_db),
                         admin: User = Depends(require())):
    ctx = await _product_form_context(db)
    return render(request, "admin_writeoff.html", admin, article=request.query_params.get("article"),
                  cities=ctx["cities"])


@router.post("/products/writeoff")
async def admin_writeoff_post(
    article: str = Form(...), quantity: int = Form(...), city_id: int = Form(...), notes: str = Form(""),
    db: AsyncSession = Depends(get_db), admin: User = Depends(require()),
):
    url = f"/admin/products/writeoff?article={quote(article)}"
    product = (await db.execute(select(Product).where(Product.sku == article.strip()))).scalar_one_or_none()
    if not product:
        return back(url, error="Товар с таким артикулом не найден")
    if quantity <= 0:
        return back(url, error="Количество должно быть больше нуля")
    stock = (
        await db.execute(select(Stock).where(Stock.product_id == product.id, Stock.city_id == city_id).with_for_update())
    ).scalar_one_or_none()
    if not stock or stock.quantity < quantity:
        return back(url, error=f"На складе только {stock.quantity if stock else 0} шт.")
    stock.quantity -= quantity
    db.add(InventoryTransaction(
        product_id=product.id, user_id=admin.id, quantity=-quantity, type=TransactionType.WRITE_OFF,
        from_city_id=city_id, notes=notes.strip() or None,
    ))
    await db.commit()
    return back("/admin/inventory", msg=f"Списано {quantity} шт. «{product.name}»")


@router.get("/products/{product_id}", response_class=HTMLResponse)
async def product_edit(product_id: int, request: Request, db: AsyncSession = Depends(get_db),
                       admin: User = Depends(require())):
    product = (
        await db.execute(select(Product).options(selectinload(Product.stocks)).where(Product.id == product_id))
    ).scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="Товар не найден")
    mp = (await db.execute(select(MobaPrice).where(MobaPrice.sku == product.sku))).scalars().first()
    history = (
        await db.execute(
            select(InventoryTransaction)
            .options(joinedload(InventoryTransaction.user), joinedload(InventoryTransaction.supplier))
            .where(InventoryTransaction.product_id == product_id)
            .order_by(InventoryTransaction.created_at.desc()).limit(20)
        )
    ).scalars().all()
    return render(request, "admin_product_form.html", admin, product=product, mp=mp,
                  stocks={s.city_id: s.quantity for s in product.stocks}, history=history,
                  **await _product_form_context(db))


@router.post("/products/{product_id}")
async def product_update(product_id: int, request: Request, db: AsyncSession = Depends(get_db),
                         admin: User = Depends(require())):
    product = (await db.execute(select(Product).where(Product.id == product_id))).scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="Товар не найден")
    form = await request.form()
    url = f"/admin/products/{product_id}"
    try:
        name = (form.get("name") or "").strip()
        if not name:
            raise ValueError("Название не может быть пустым")
        opt = round_money(_decimal(form.get("price_opt"), "ОПТ / Moba", allow_empty=False))
        if opt <= 0:
            raise ValueError("Цена должна быть больше нуля")
        cost = _decimal(form.get("purchase_price"), "Себестоимость")
        calculated = reference_prices(opt, purchase_price=cost)
        base, card, opt = calculated["base"], calculated["card"], calculated["opt"]
        threshold = int(form.get("critical_stock_threshold") or 0)
        if threshold < 0:
            raise ValueError("Порог не может быть отрицательным")
        new_stocks = {int(k[6:]): int(v) for k, v in form.items() if k.startswith("stock_") and str(v).strip() != ""}
        if any(v < 0 for v in new_stocks.values()):
            raise ValueError("Остаток не может быть отрицательным")
        city_ids = set((await db.execute(select(City.id))).scalars())
        new_stocks = {cid: v for cid, v in new_stocks.items() if cid in city_ids}
        supplier_id = await _supplier_id(db, form.get("supplier_id"))
        category_id = int(form.get("category_id") or product.category_id)
        if not (await db.execute(select(Category.id).where(Category.id == category_id))).first():
            raise ValueError("Выберите категорию")
    except (ValueError, TypeError) as e:
        return back(url, error=str(e) or "Проверьте поля формы")

    product.name = name
    product.description = (form.get("description") or "").strip() or None
    product.category_id = category_id
    product.supplier_id = supplier_id
    product.price_base, product.price_opt, product.price_card = base, opt, card
    product.price_super_opt = calculated["super_opt"]
    product.purchase_price = cost
    product.critical_stock_threshold = threshold

    mp = (await db.execute(select(MobaPrice).where(MobaPrice.sku == product.sku))).scalars().first()
    if mp:
        mp.name, mp.price, mp.our_base, mp.our_opt, mp.our_card = name, opt, base, opt, card
        mp.our_super_opt = calculated["super_opt"]
    else:
        db.add(MobaPrice(sku=product.sku, name=name, price=opt, our_base=base, our_card=card, our_opt=opt, our_super_opt=calculated["super_opt"]))

    # Ручная корректировка остатков по городам (как «Изменить остаток вручную» в боте)
    current = {s.city_id: s for s in (await db.execute(select(Stock).where(Stock.product_id == product_id))).scalars()}
    for city_id, qty in new_stocks.items():
        stock = current.get(city_id)
        old = stock.quantity if stock else 0
        if qty == old:
            continue
        if stock:
            stock.quantity = qty
        else:
            db.add(Stock(product_id=product_id, city_id=city_id, quantity=qty))
        db.add(InventoryTransaction(
            product_id=product_id, user_id=admin.id, type=TransactionType.ADJUSTMENT, quantity=qty - old,
            to_city_id=city_id if qty > old else None, from_city_id=city_id if qty < old else None,
            notes=f"Корректировка из админки ({old} → {qty})",
        ))
    await db.commit()
    catalog_tree.invalidate()
    return back(url, msg="Изменения сохранены")


@router.post("/products/{product_id}/toggle")
async def product_toggle(product_id: int, db: AsyncSession = Depends(get_db),
                         admin: User = Depends(require())):
    product = (await db.execute(select(Product).where(Product.id == product_id))).scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="Товар не найден")
    product.is_active = 0 if product.is_active else 1
    await db.commit()
    catalog_tree.invalidate()
    return back(f"/admin/products/{product_id}",
                msg="Товар возвращён в каталог" if product.is_active else "Товар скрыт из каталога")


# ───────────────────────── Склад ─────────────────────────


@router.get("/inventory", response_class=HTMLResponse)
async def admin_inventory(request: Request, type: str = "all", page: int = 1, days: int = 30,
                          db: AsyncSession = Depends(get_db), admin: User = Depends(require())):
    T = InventoryTransaction
    cond = []
    if type in TransactionType.__members__:
        cond.append(T.type == TransactionType[type])
    else:
        type = "all"
    total = (await db.execute(select(func.count(T.id)).where(*cond))).scalar_one()
    pages, page = _pages(total, page)
    transactions = (
        await db.execute(
            select(T)
            .options(joinedload(T.product), joinedload(T.user), joinedload(T.to_city),
                     joinedload(T.from_city), joinedload(T.supplier))
            .where(*cond)
            .order_by(T.created_at.desc())
            .offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)
        )
    ).scalars().all()
    days = days if days in (7, 30, 90) else 30
    return render(request, "admin_inventory.html", admin, transactions=transactions, type=type,
                  TransactionType=TransactionType, page=page, pages=pages, total=total, days=days,
                  report=await analytics.movement_report(db, days=days))


@router.get("/alerts", response_class=HTMLResponse)
async def admin_alerts(request: Request, db: AsyncSession = Depends(get_db),
                       admin: User = Depends(require())):
    st = (select(Stock.product_id, func.sum(Stock.quantity).label("qty")).group_by(Stock.product_id).subquery())
    qty = func.coalesce(st.c.qty, 0)
    rows = (
        await db.execute(
            select(Product, qty.label("qty"))
            .outerjoin(st, st.c.product_id == Product.id)
            .where(Product.is_active == 1, qty <= Product.critical_stock_threshold, analytics.stocked())
            .order_by(qty, Product.name)
            .limit(500)
        )
    ).all()
    return render(request, "admin_alerts.html", admin, rows=rows)


@router.get("/forecast", response_class=HTMLResponse)
async def admin_forecast(request: Request, db: AsyncSession = Depends(get_db),
                         admin: User = Depends(require())):
    forecast = await analytics.procurement_forecast(db)
    counts = {k: sum(1 for f in forecast if f["urgency"] == k) for k in ("EMPTY", "HIGH", "MEDIUM")}
    return render(request, "admin_forecast.html", admin, forecast=forecast[:200], counts=counts)


@router.get("/suppliers", response_class=HTMLResponse)
async def admin_suppliers(request: Request, db: AsyncSession = Depends(get_db),
                          admin: User = Depends(require())):
    return render(request, "admin_suppliers.html", admin, rows=await analytics.supplier_analysis(db))


@router.post("/suppliers/add")
async def add_supplier(name: str = Form(...), contact_info: str = Form(""), average_delivery_days: int = Form(0),
                       db: AsyncSession = Depends(get_db), admin: User = Depends(require())):
    if not name.strip():
        return back("/admin/suppliers", error="Укажите название поставщика")
    db.add(Supplier(name=name.strip(), contact_info=contact_info.strip() or None,
                    average_delivery_days=max(0, average_delivery_days)))
    await db.commit()
    return back("/admin/suppliers", msg="Поставщик добавлен")


# ───────────────────────── Аналитика и финансы ─────────────────────────


@router.get("/analytics", response_class=HTMLResponse)
async def admin_analytics(request: Request, db: AsyncSession = Depends(get_db),
                          admin: User = Depends(require())):
    return render(request, "admin_analytics.html", admin,
                  kpi=await analytics.sales_kpi(db), abc=await analytics.abc_analysis(db),
                  top_clients=await analytics.top_clients(db))


@router.get("/finance", response_class=HTMLResponse)
async def admin_finance(request: Request, db: AsyncSession = Depends(get_db),
                        admin: User = Depends(require())):
    return render(request, "admin_finance.html", admin,
                  fin=await analytics.finance_summary(db), stock=await analytics.stock_summary(db),
                  top_margin=await analytics.top_margin_products(db))


# ───────────────────────── Пользователи и опт ─────────────────────────


@router.get("/users", response_class=HTMLResponse)
async def admin_users(request: Request, q: str = "", page: int = 1,
                      db: AsyncSession = Depends(get_db), admin: User = Depends(require())):
    cond = [User.username.ilike(f"%{q.strip()}%")] if q.strip() else []
    total = (await db.execute(select(func.count(User.id)).where(*cond))).scalar_one()
    pages, page = _pages(total, page)
    users = (
        await db.execute(select(User).where(*cond).order_by(User.id).offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE))
    ).scalars().all()
    return render(request, "admin_users.html", admin, users=users, UserRole=UserRole, q=q,
                  page=page, pages=pages, total=total)


@router.post("/users/{target_id}/role")
async def change_user_role(target_id: int, role: str = Form(...), db: AsyncSession = Depends(get_db),
                           admin: User = Depends(require())):
    if role not in UserRole.__members__:
        return back("/admin/users", error="Неизвестная роль")
    target = (await db.execute(select(User).where(User.id == target_id))).scalar_one_or_none()
    if not target:
        return back("/admin/users", error="Пользователь не найден")
    if target.id == admin.id and role != "ADMIN":
        return back("/admin/users", error="Нельзя снять роль администратора с самого себя")
    target.role = UserRole[role]
    target.is_admin = target.role == UserRole.ADMIN
    await db.commit()
    return back("/admin/users", msg=f"Роль пользователя {target.username} изменена")


@router.post("/users/{target_id}/price-type")
async def change_price_type(target_id: int, price_type: str = Form(...), db: AsyncSession = Depends(get_db),
                            admin: User = Depends(require())):
    if price_type not in ("retail", "wholesale", "super_wholesale"):
        return back("/admin/users", error="Неизвестный тариф")
    target = await db.get(User, target_id)
    if not target:
        return back("/admin/users", error="Пользователь не найден")
    target.price_type = price_type
    await db.commit()
    return back("/admin/users", msg="Тариф цен сохранён")


@router.post("/users/{target_id}/password-reset", response_class=HTMLResponse)
async def customer_password_reset(target_id: int, request: Request, identity_checked: str = Form(""),
                                  db: AsyncSession = Depends(get_db), admin: User = Depends(require())):
    if identity_checked != "1":
        return back("/admin/users", error="Сначала проверьте, что обращается владелец аккаунта")
    target = await db.get(User, target_id)
    if not target or target.is_superuser or not target.can_sign_in:
        return back("/admin/users", error="Ссылка доступна только для активного аккаунта покупателя")
    key = f"support-reset:admin:{admin.id}"
    if cart.redis_client.incr(key) == 1:
        cart.redis_client.expire(key, 900)
    if int(cart.redis_client.get(key)) > 10:
        raise HTTPException(status_code=429, detail="Подождите 15 минут перед выдачей новых ссылок")
    token = accounts.new_password_reset(target)
    logging.getLogger(__name__).warning("Customer password reset issued by admin=%s for user=%s", admin.id, target.id)
    return render(request, "admin_password_reset.html", admin, target=target,
                  reset_url=telegram.site(f"/auth/reset?token={token}"))


@router.get("/wholesale", response_class=HTMLResponse)
async def admin_wholesale(request: Request, show: str = "pending", db: AsyncSession = Depends(get_db),
                          admin: User = Depends(require())):
    query = select(WholesaleApplication).options(joinedload(WholesaleApplication.user))
    if show != "all":
        show = "pending"
        query = query.where(WholesaleApplication.status == ApplicationStatus.PENDING)
    apps = (await db.execute(query.order_by(WholesaleApplication.created_at.desc()))).scalars().all()
    return render(request, "admin_wholesale.html", admin, applications=apps, show=show)


@router.post("/wholesale/{app_id}/status")
async def update_wholesale_status(app_id: int, status: str = Form(...), db: AsyncSession = Depends(get_db),
                                  admin: User = Depends(require())):
    if status not in ("APPROVED", "REJECTED"):
        return back("/admin/wholesale", error="Неизвестное решение")
    application = (
        await db.execute(
            select(WholesaleApplication).options(joinedload(WholesaleApplication.user))
            .where(WholesaleApplication.id == app_id)
        )
    ).scalar_one_or_none()
    if not application or application.status != ApplicationStatus.PENDING:
        return back("/admin/wholesale", error="Заявка не найдена или уже обработана")
    application.status = ApplicationStatus[status]
    # Оптовые цены получает розничный покупатель; администратора не понижаем
    if application.status == ApplicationStatus.APPROVED and application.user.role == UserRole.RETAIL:
        application.user.role = UserRole.WHOLESALE
    await db.commit()
    telegram.fire(telegram.notify_wholesale_result(application.user_id, application.status == ApplicationStatus.APPROVED))
    return back("/admin/wholesale", msg=f"Заявка #{app_id} {'одобрена' if status == 'APPROVED' else 'отклонена'}")


# ───────────────────────── Рассылки ─────────────────────────


@router.get("/mailing", response_class=HTMLResponse)
async def admin_mailing(request: Request, db: AsyncSession = Depends(get_db), admin: User = Depends(require())):
    mailings = (await db.execute(select(Mailing).order_by(Mailing.created_at.desc()))).scalars().all()
    segments = {
        "all": (await db.execute(select(func.count(User.id)))).scalar_one(),
        "wholesale": (await db.execute(select(func.count(User.id)).where(User.role == UserRole.WHOLESALE))).scalar_one(),
        "retail": (await db.execute(select(func.count(User.id)).where(User.role == UserRole.RETAIL))).scalar_one(),
    }
    return render(request, "admin_mailing.html", admin, mailings=mailings, segments=segments)


@router.post("/mailing/create")
async def create_mailing(subject: str = Form(...), body_template: str = Form(...), target_segment: str = Form(...),
                         db: AsyncSession = Depends(get_db), admin: User = Depends(require())):
    if target_segment not in ("all", "wholesale", "retail"):
        return back("/admin/mailing", error="Неизвестный сегмент")
    if not subject.strip() or not body_template.strip():
        return back("/admin/mailing", error="Заполните тему и текст рассылки")
    db.add(Mailing(subject=subject.strip(), body_template=body_template.strip(), target_segment=target_segment))
    await db.commit()
    return back("/admin/mailing", msg="Черновик рассылки сохранён")


# ───────────────────────── Настройки цен ─────────────────────────

PRICE_SETTINGS = {
    "COEF_OPT": "Коэффициент оптовой цены к базовой",
    "BASE_FROM": "Цена Moba для розницы: regular или discount",
}


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, db: AsyncSession = Depends(get_db), admin: User = Depends(require())):
    configs = {c.key: c.value for c in (await db.execute(select(SystemConfig))).scalars().all()}
    return render(request, "admin_settings.html", admin, configs=configs)


@router.post("/settings")
async def update_settings(request: Request, db: AsyncSession = Depends(get_db), admin: User = Depends(require())):
    updated = await recalculate_all(db)
    return back("/admin/settings", msg=f"Пересчитано цен: {updated}")


# ───────────────────────── Интеграция с 1С ─────────────────────────


@router.get("/integration-1c", response_class=HTMLResponse)
async def integration_1c(request: Request, db: AsyncSession = Depends(get_db), admin: User = Depends(require())):
    from app.config import settings

    stats = {
        "products_linked": (await db.execute(select(func.count(Product.id)).where(Product.onec_id.isnot(None)))).scalar_one(),
        "products_total": (await db.execute(select(func.count(Product.id)))).scalar_one(),
        "orders_pending": (
            await db.execute(select(func.count(Order.id)).where(
                Order.onec_exported_at.is_(None), Order.status != OrderStatus.CANCELLED))
        ).scalar_one(),
        "orders_exported": (await db.execute(select(func.count(Order.id)).where(Order.onec_exported_at.isnot(None)))).scalar_one(),
    }
    cities = (await db.execute(select(City).order_by(City.id))).scalars().all()
    return render(request, "admin_integration_1c.html", admin, s=settings, stats=stats, cities=cities,
                  exchange_url=str(request.base_url).rstrip("/") + "/1c_exchange")


@router.post("/integration-1c/warehouses")
async def integration_1c_warehouses(request: Request, db: AsyncSession = Depends(get_db), admin: User = Depends(require())):
    form = await request.form()
    for city in (await db.execute(select(City))).scalars():
        value = (form.get(f"wh_{city.id}") or "").strip()
        city.onec_warehouse_id = value or None
    await db.commit()
    return back("/admin/integration-1c", msg="Сопоставление складов сохранено")


# ───────────────────────── Обращения (чат с покупателями) ─────────────────────────


@router.get("/chats", response_class=HTMLResponse)
async def admin_chats(request: Request, show: str = "open", db: AsyncSession = Depends(get_db),
                      admin: User = Depends(require())):
    query = select(ChatThread).options(joinedload(ChatThread.user), joinedload(ChatThread.manager))
    if show == "closed":
        query = query.where(ChatThread.status == ChatStatus.CLOSED)
    elif show == "mine":
        query = query.where(ChatThread.assigned_to == admin.id, ChatThread.status == ChatStatus.OPEN)
    else:
        show = "open"
        query = query.where(ChatThread.status == ChatStatus.OPEN)
    threads = (await db.execute(query.order_by(ChatThread.unread_staff.desc(), ChatThread.last_message_at.desc()).limit(200))).scalars().all()
    # Последнее сообщение каждого диалога — для превью в списке
    last = {}
    if threads:
        sub = (select(func.max(ChatMessage.id)).where(ChatMessage.thread_id.in_([t.id for t in threads]))
               .group_by(ChatMessage.thread_id))
        for m in (await db.execute(select(ChatMessage).where(ChatMessage.id.in_(sub)))).scalars():
            last[m.thread_id] = m
    counts = dict((await db.execute(select(ChatThread.status, func.count(ChatThread.id)).group_by(ChatThread.status))).all())
    return render(request, "_admin_chat_list.html" if request.query_params.get("partial") else "admin_chats.html",
                  admin, threads=threads, last=last, show=show, counts={k.name: v for k, v in counts.items()})


async def _thread_or_404(db: AsyncSession, thread_id: int) -> ChatThread:
    thread = (
        await db.execute(select(ChatThread).options(joinedload(ChatThread.user), joinedload(ChatThread.manager))
                         .where(ChatThread.id == thread_id))
    ).scalar_one_or_none()
    if not thread:
        raise HTTPException(status_code=404, detail="Обращение не найдено")
    return thread


@router.get("/chats/{thread_id}", response_class=HTMLResponse)
async def admin_chat(thread_id: int, request: Request, db: AsyncSession = Depends(get_db),
                     admin: User = Depends(require())):
    thread = await _thread_or_404(db, thread_id)
    cart_rows, orders = [], []
    if thread.user_id:
        cart_rows = (
            await db.execute(
                select(UserCart.quantity, MobaPrice)
                .join(Product, Product.id == UserCart.product_id)
                .join(MobaPrice, MobaPrice.sku == Product.sku)
                .where(UserCart.user_id == thread.user_id)
            )
        ).all()
        orders = (
            await db.execute(select(Order).where(Order.user_id == thread.user_id).order_by(Order.id.desc()).limit(5))
        ).scalars().all()
    return render(request, "admin_chat.html", admin, thread=thread, cart_rows=cart_rows, orders=orders,
                  quick_replies=chat.QUICK_REPLIES, OrderStatus=OrderStatus)


@router.get("/api/events")
async def admin_events(db: AsyncSession = Depends(get_db), admin: User = Depends(require())):
    """Что нового — для обновления админки без перезагрузки (опрос раз в несколько секунд)."""
    last_order = (await db.execute(select(Order.id, Order.customer_name, Order.total_amount)
                                   .order_by(Order.id.desc()).limit(1))).first()
    last_msg = (await db.execute(select(ChatMessage.id, ChatMessage.thread_id, ChatMessage.text)
                                 .where(ChatMessage.sender == "client").order_by(ChatMessage.id.desc()).limit(1))).first()
    return JSONResponse({
        "chats": await chat.unread_for_staff(db),
        "orders": (await db.execute(select(func.count(Order.id)).where(Order.status == OrderStatus.PENDING))).scalar_one(),
        "wholesale": (await db.execute(select(func.count(WholesaleApplication.id))
                                       .where(WholesaleApplication.status == ApplicationStatus.PENDING))).scalar_one(),
        "last_order": {"id": last_order[0], "name": last_order[1], "total": float(last_order[2])} if last_order else None,
        "last_msg": {"id": last_msg[0], "thread": last_msg[1], "text": last_msg[2][:120]} if last_msg else None,
    })


@router.get("/api/chats/unread")
async def admin_chats_unread(db: AsyncSession = Depends(get_db), admin: User = Depends(require())):
    return JSONResponse({"count": await chat.unread_for_staff(db)})


@router.get("/api/chats/{thread_id}/messages")
async def admin_chat_messages(thread_id: int, after: int = 0, db: AsyncSession = Depends(get_db),
                              admin: User = Depends(require())):
    thread = await _thread_or_404(db, thread_id)
    msgs = await chat.messages_after(db, thread.id, after)
    names = {u.id: u.username for u in (await db.execute(
        select(User).where(User.id.in_({m.author_id for m in msgs if m.author_id})))).scalars()}
    if thread.unread_staff:
        thread.unread_staff = 0
        await db.commit()
    return JSONResponse({"messages": [chat.serialize(m, names) for m in msgs], "status": thread.status.name,
                         "client_unread": thread.unread_client})


@router.post("/api/chats/{thread_id}/send")
async def admin_chat_send(thread_id: int, request: Request, db: AsyncSession = Depends(get_db),
                          admin: User = Depends(require())):
    thread = await _thread_or_404(db, thread_id)
    try:
        data = await request.json()
        msg = await chat.add_message(db, thread, "staff", str(data.get("text") or ""), author_id=admin.id)
    except (ValueError, chat.ChatError) as e:
        return JSONResponse({"ok": False, "error": str(e) or "Некорректный запрос"}, status_code=400)
    if thread.assigned_to is None:
        thread.assigned_to = admin.id  # кто ответил первым — тот и ведёт обращение
    if thread.status == ChatStatus.CLOSED:
        thread.status = ChatStatus.OPEN
    await db.commit()
    telegram.fire(telegram.notify_client_reply(thread.id, msg.text))
    return JSONResponse({"ok": True, "message": chat.serialize(msg, {admin.id: admin.username})})


@router.post("/chats/{thread_id}/status")
async def admin_chat_status(thread_id: int, status: str = Form(...), db: AsyncSession = Depends(get_db),
                            admin: User = Depends(require())):
    thread = await _thread_or_404(db, thread_id)
    if status not in ChatStatus.__members__:
        return back(f"/admin/chats/{thread_id}", error="Неизвестный статус")
    thread.status = ChatStatus[status]
    note = "Обращение закрыто. Если остались вопросы — просто напишите." if status == "CLOSED" else "Обращение снова открыто"
    await chat.add_message(db, thread, "system", note)
    thread.unread_client = 0
    await db.commit()
    return back("/admin/chats" if status == "CLOSED" else f"/admin/chats/{thread_id}",
                msg="Обращение закрыто" if status == "CLOSED" else "Обращение открыто")


@router.post("/chats/{thread_id}/assign")
async def admin_chat_assign(thread_id: int, db: AsyncSession = Depends(get_db), admin: User = Depends(require())):
    thread = await _thread_or_404(db, thread_id)
    thread.assigned_to = admin.id
    await db.commit()
    return back(f"/admin/chats/{thread_id}", msg="Обращение назначено на вас")


# ───────────────────────── Экспорт ─────────────────────────

EXPORTS = {
    "orders": ("Заказы", analytics.export_orders_csv),
    "stock": ("Остатки склада", analytics.export_stock_csv),
    "movements": ("Движение товара (60 дней)", analytics.export_movements_csv),
    "analytics": ("Аналитика продаж: KPI, клиенты, ABC, прогноз", analytics.export_analytics_csv),
}


@router.get("/export", response_class=HTMLResponse)
async def export_page(request: Request, admin: User = Depends(require())):
    return render(request, "admin_export.html", admin, exports=EXPORTS)


@router.get("/export/{kind}.csv")
async def export_csv(kind: str, db: AsyncSession = Depends(get_db), admin: User = Depends(require())):
    if kind not in EXPORTS:
        raise HTTPException(status_code=404, detail="Неизвестный тип выгрузки")
    content = await EXPORTS[kind][1](db)
    filename = f"{kind}_{datetime.now():%Y%m%d}.csv"
    return Response(
        content="﻿" + content,  # BOM — Excel корректно откроет UTF-8
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/export/csv")
async def export_csv_legacy(admin: User = Depends(require())):
    """Старая ссылка «Экспорт заказов»."""
    return RedirectResponse(url="/admin/export/orders.csv", status_code=303)


@router.get('/integration-1c/prices.csv')
async def export_onec_prices(db: AsyncSession = Depends(get_db), admin: User = Depends(require())):
    import csv
    import io
    from app.services.onec_exchange import price_feed
    def _csv_cell(value):
        return "'" + value if value[:1] in ('=', '+', '-', '@') else value
    stream = io.StringIO(newline='')
    writer = csv.writer(stream, delimiter=';')
    writer.writerow(['Ид 1С', 'Артикул / штрихкод', 'Номенклатура', 'Розница', 'ОПТ', 'СуперОПТ'])
    for row in (await price_feed(db))['items']:
        writer.writerow([_csv_cell(row['id'] or ''), _csv_cell(row['sku']), _csv_cell(row['name']),
                         row['retail'], row['wholesale'], row['super_wholesale'] or ''])
    return Response('\ufeff' + stream.getvalue(), media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition': 'attachment; filename="parts-prices-1c.csv"', 'Cache-Control':'no-store'})


@router.get('/orders/{order_id:int}/invoice', response_class=HTMLResponse)
@router.get('/orders/{order_id:int}/delivery-note', response_class=HTMLResponse)
async def print_invoice(order_id: int, request: Request, db: AsyncSession = Depends(get_db),
                        admin: User = Depends(require())):
    from app.services.invoice import invoice_data, money
    order = (await db.execute(select(Order).where(Order.id == order_id)
                             .options(selectinload(Order.items).selectinload(OrderItem.product)))).scalar_one_or_none()
    if not order: raise HTTPException(404, 'Заказ не найден')
    try: data = invoice_data(order)
    except ValueError as e: raise HTTPException(409, str(e))
    return templates.TemplateResponse(request=request, name='invoice.html', context={
        'invoice':data, 'money':money, 'delivery_note':request.url.path.endswith('/delivery-note')})
