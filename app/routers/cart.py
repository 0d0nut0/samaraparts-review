from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.templating import templates
from app.services import cart

router = APIRouter(prefix="/cart", tags=["cart"])



@router.get("/count")
async def cart_count(request: Request, db: AsyncSession = Depends(get_db)):
    """Возвращает текущее количество товаров в корзине (используется для обновления бейджика в шапке)"""
    count = await cart.get_cart_count_async(request, db)
    return {"count": count}


@router.post("/add")
async def cart_add(
    request: Request,
    sku: str = Form(...),
    qty: int = Form(1),
    db: AsyncSession = Depends(get_db)
):
    """Добавляет товар в корзину (по умолчанию 1 шт.) и возвращает новое количество"""
    new_qty = await cart.add_to_cart(request, sku, qty, db)
    if not new_qty:
        return JSONResponse({"ok": False, "error": "Товар не найден"}, status_code=404)
    count = await cart.get_cart_count_async(request, db)
    return JSONResponse({"ok": True, "count": count})


@router.post("/set")
async def cart_set(
    request: Request,
    sku: str = Form(...),
    qty: int = Form(...),
    db: AsyncSession = Depends(get_db)
):
    """Устанавливает точное количество товара в корзине"""
    await cart.set_quantity(request, sku, qty, db)
    return RedirectResponse("/cart", status_code=303)


@router.post("/remove")
async def cart_remove(
    request: Request,
    sku: str = Form(...),
    db: AsyncSession = Depends(get_db)
):
    """Полностью удаляет конкретную позицию из корзины"""
    await cart.remove_from_cart(request, sku, db)
    return RedirectResponse("/cart", status_code=303)


@router.post("/clear")
async def cart_clear(
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """Очищает всю корзину для текущего пользователя/сессии"""
    await cart.clear_cart_async(request, db)
    return RedirectResponse("/cart", status_code=303)


@router.get("", response_class=HTMLResponse)
async def cart_page(request: Request, db: AsyncSession = Depends(get_db)):
    """Отображает страницу корзины со всеми добавленными товарами и итоговой суммой"""
    items = await cart.get_cart_items(request, db)
    totals = cart.cart_totals(items)

    return templates.TemplateResponse(
        request=request,
        name="cart.html",
        context={
            "items": items,
            "error": request.query_params.get("error"),
            **totals,
        },
    )
