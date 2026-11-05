from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.templating import templates
from app.models import City, Order, Product, Stock
from app.services import order_service, cart, telegram

router = APIRouter(prefix="/checkout", tags=["checkout"])



@router.get("", response_class=HTMLResponse)
async def checkout_page(
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """Страница оформления заказа."""
    # Проверка авторизации
    user_id = request.session.get("user_id")
    if not user_id or not request.state.user:
        return RedirectResponse(url="/auth/login", status_code=303)

    items = await cart.get_cart_items(request, db)
    if not items:
        return RedirectResponse(url="/cart", status_code=303)

    cities = (await db.execute(select(City).where(City.is_active.is_(True)).order_by(City.id))).scalars().all()

    # Сколько позиций корзины можно собрать на складе каждого города — чтобы выбрать город без отказа
    need = {i["sku"]: i["qty"] for i in items}
    rows = (
        await db.execute(
            select(Product.sku, Stock.city_id, Stock.quantity)
            .join(Stock, Stock.product_id == Product.id)
            .where(Product.sku.in_(list(need)))
        )
    ).all()
    short = {c.id: len(need) for c in cities}
    for sku, city_id, qty in rows:
        if city_id in short and qty >= need[sku]:
            short[city_id] -= 1
    selected = getattr(request.state, "city", None)
    default_city = selected.id if selected and short.get(selected.id) == 0 else next(
        (c.id for c in cities if short[c.id] == 0), selected.id if selected else None)

    # Подставляем имя и телефон из последнего заказа
    last = (
        await db.execute(select(Order).where(Order.user_id == user_id).order_by(Order.id.desc()).limit(1))
    ).scalar_one_or_none()

    return templates.TemplateResponse(
        request=request,
        name="checkout.html",
        context={
            "items": items, "cities": cities, "short": short, "default_city": default_city,
            "prefill": {
                "name": last.customer_name if last else (request.state.user.full_name or "") if request.state.user else "",
                "phone": last.customer_phone if last else (request.state.user.phone or "") if request.state.user else "",
            },
            **cart.cart_totals(items),
        },
    )


@router.get("/summary", response_class=HTMLResponse)
async def checkout_summary(request: Request, delivery_method: str = "pickup", db: AsyncSession = Depends(get_db)):
    if not request.state.user:
        return RedirectResponse(url="/auth/login?next=/checkout", status_code=303)
    items = await cart.get_cart_items(request, db, pickup=delivery_method == "pickup")
    return templates.TemplateResponse(request=request, name="_checkout_summary.html", context={
        "items": items, "delivery_method": delivery_method, **cart.cart_totals(items)})


@router.post("/process")
async def process_checkout(
    request: Request,
    customer_name: str = Form(...),
    customer_phone: str = Form(...),
    address: str = Form(""),
    delivery_method: str = Form("pickup"),
    city_id: int = Form(...),
    comment: str = Form(""),
    consent: str = Form(""),
    db: AsyncSession = Depends(get_db)
):
    """Обработка создания заказа."""
    user_id = request.session.get("user_id")
    if not user_id or not request.state.user:
        return RedirectResponse(url="/auth/login", status_code=303)

    if delivery_method not in ("pickup", "delivery"):
        return RedirectResponse(url="/cart?error=" + quote("Выберите способ получения заказа"), status_code=303)
    address = (address or "").strip()[:500]
    if delivery_method == "delivery" and not address:
        return RedirectResponse(url="/cart?error=" + quote("Укажите адрес доставки"), status_code=303)
    if delivery_method == "pickup":
        address = ""
    items = await cart.get_cart_items(request, db, pickup=delivery_method == "pickup")
    if not items:
        return RedirectResponse(url="/cart", status_code=303)

    if not consent:
        return RedirectResponse(url="/cart?error=" + quote("Нужно согласие на обработку персональных данных"), status_code=303)
    customer_name, customer_phone = customer_name.strip()[:200], customer_phone.strip()[:50]
    if not customer_name or sum(ch.isdigit() for ch in customer_phone) < 5:
        return RedirectResponse(url="/cart?error=" + quote("Укажите имя и телефон для связи"), status_code=303)

    # Цена берётся с сервера (get_cart_items), а не из формы
    cart_data = [{"sku": item["sku"], "qty": item["qty"], "price": item["price"]} for item in items]
    user = request.state.user

    try:
        order = await order_service.create_order(
            db=db,
            user_id=user_id,
            city_id=city_id,
            cart_items=cart_data,
            customer_name=customer_name,
            customer_phone=customer_phone,
            address=(address or "").strip()[:500] or None,
            wholesale=bool(user and user.is_wholesale),
            comment=comment.strip()[:1000] or None,
        )
        # Очистка корзины — в той же транзакции, что и заказ
        await cart.clear_cart_async(request, db, commit=False)
        if user and not user.phone:
            user.phone = customer_phone  # подставим в следующий раз и покажем менеджеру
        await db.commit()
    except order_service.OrderError as e:
        return RedirectResponse(url=f"/cart?error={quote(str(e))}", status_code=303)

    telegram.fire(telegram.notify_new_order(order.id))

    return RedirectResponse(url=f"/checkout/success?order={order.id}", status_code=303)


@router.get("/success", response_class=HTMLResponse)
async def checkout_success(request: Request):
    """Страница успешного оформления."""
    return templates.TemplateResponse(
        request=request,
        name="success.html",
        context={
            "order_id": request.query_params.get("order"),
            "message": "Заказ успешно оформлен! Мы свяжемся с вами в ближайшее время.",
        },
    )
