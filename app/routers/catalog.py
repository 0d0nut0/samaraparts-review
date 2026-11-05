import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from types import SimpleNamespace

from sqlalchemy import select, func, exists, case
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.templating import templates
from app.models import MobaPrice, Product, Stock, City
from app.services import catalog_tree, search
from app.services.pricing import buyer_prices

router = APIRouter(prefix="/catalog", tags=["catalog"])


PAGE_SIZE = 24
SUGGEST_LIMIT = 8


def _visible_filter():
    """Показываем все товары, кроме скрытых в админке (Product.is_active = 0); без цены — «Цена по запросу»."""
    hidden = exists().where(Product.sku == MobaPrice.sku, Product.is_active == 0)
    return [~hidden]


def _category_filter(category_ids: list[int]):
    return exists().where(Product.sku == MobaPrice.sku, Product.category_id.in_(category_ids))


# Примеры запросов под строкой поиска — показывают, что можно искать моделью и деталью
SEARCH_EXAMPLES = ["iPhone 11 дисплей", "realme c67 акб", "honor 10i дисплей в рамке", "iPhone 13 pro max шлейф", "A51 крышка"]


async def _stock_map(db: AsyncSession, skus: list[str], city_id: int | None) -> dict:
    """sku → SimpleNamespace(city=остаток в выбранном городе, total=всего) для карточек."""
    if not skus:
        return {}
    in_city = func.sum(case((Stock.city_id == city_id, Stock.quantity), else_=0)) if city_id else func.sum(0)
    rows = (
        await db.execute(
            select(Product.sku, in_city, func.sum(Stock.quantity))
            .join(Stock, Stock.product_id == Product.id)
            .where(Product.sku.in_(skus))
            .group_by(Product.sku)
        )
    ).all()
    return {sku: SimpleNamespace(city=int(c or 0), total=int(t or 0)) for sku, c, t in rows}


def _related_order(name: str) -> list:
    """Сначала товары для той же модели: совпадение латинских слов названия («iphone 11»)."""
    words = search.parse(name)["latin"][:2]  # обычно это бренд/линейка и номер модели
    if not words:
        return [MobaPrice.id]
    phrase = r"\m" + r"[\s/]+".join(words) + r"\M"
    return [case((MobaPrice.name.op("~*")(phrase), 0), else_=1), MobaPrice.id]


def _card_context(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    return {"city": getattr(request.state, "city", None), "wholesale": bool(user and user.is_wholesale)}


def _parse_specs(raw: str | None) -> list[dict]:
    """specifications хранится JSON-строкой: список {key, value} или словарь."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if isinstance(data, dict):
        return [{"key": k, "value": v} for k, v in data.items()]
    if isinstance(data, list):
        return [s for s in data if isinstance(s, dict) and "key" in s]
    return []


@router.get("", response_class=HTMLResponse)
async def catalog_list(
    request: Request,
    q: str | None = None,
    cat: str | None = None,
    page: int = 1,
    db: AsyncSession = Depends(get_db),
):
    """
    Каталог: разделы, фильтр по категории (?cat=slug, включая подкатегории), поиск (?q=) и пагинация.
    """
    page = max(page, 1)
    tree = await catalog_tree.get_tree(db)
    node = tree.by_slug.get(cat) if cat else None

    conditions = _visible_filter()
    if node:
        conditions.append(_category_filter(tree.descendant_ids(node)))
    if q and q.strip():
        conditions.extend(search.conditions(q))

    total = (await db.execute(select(func.count()).select_from(MobaPrice).where(*conditions))).scalar_one()
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, total_pages)

    # Без поиска — сначала новые поступления
    order = search.order_by(q) if q and q.strip() else [search.availability().desc(), MobaPrice.id.desc()]
    query = (
        select(MobaPrice)
        .where(*conditions)
        .order_by(*order)
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
    )
    products = (await db.execute(query)).scalars().all()
    ctx = _card_context(request)
    stocks = await _stock_map(db, [p.sku for p in products], ctx["city"].id if ctx["city"] else None)

    return templates.TemplateResponse(
        request=request,
        name="catalog.html",
        context={
            "products": products,
            "total": total,
            "total_pages": total_pages,
            "page": page,
            "q": q,
            "cat": node.slug if node else None,
            "node": node,
            "path": tree.path(node) if node else [],
            "sections": tree.sections,
            "stocks": stocks,
            "examples": SEARCH_EXAMPLES,
            **ctx,
        },
    )


@router.get("/suggest")
async def catalog_suggest(request: Request, q: str = "", db: AsyncSession = Depends(get_db)):
    """Подсказки для строки поиска: до 8 товаров с ценой текущего пользователя."""
    if len(q.strip()) < 2:
        return JSONResponse({"items": []})
    rows = (
        await db.execute(
            select(MobaPrice)
            .where(*_visible_filter(), *search.conditions(q))
            .order_by(*search.order_by(q))
            .limit(SUGGEST_LIMIT)
        )
    ).scalars().all()
    user = getattr(request.state, "user", None)
    wholesale = bool(user and user.is_wholesale)
    return JSONResponse({"items": [
        {
            "sku": p.sku,
            "name": p.name,
            "price": float(price) if (price := buyer_prices(p, wholesale, authenticated=bool(user), price_type=user.effective_price_type if user else None)["price"]) is not None else None,
            "url": f"/catalog/{p.sku}",
        }
        for p in rows
    ]})


@router.get("/{sku}", response_class=HTMLResponse)
async def product_detail(
    request: Request,
    sku: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Страница отдельного товара.
    """
    moba_price = (await db.execute(select(MobaPrice).where(MobaPrice.sku == sku))).scalars().first()
    product = (await db.execute(select(Product).where(Product.sku == sku))).scalar_one_or_none()

    if not moba_price or (product and not product.is_active):
        return templates.TemplateResponse(
            request=request,
            name="404.html",
            context={"title": "Товар не найден", "detail": f"Артикул {sku} отсутствует в каталоге."},
            status_code=404,
        )

    # Остатки по всем городам (Stock + City)
    stocks = []
    path = []
    if product:
        stock_stmt = (
            select(Stock, City)
            .join(City, City.id == Stock.city_id)
            .where(Stock.product_id == product.id)
            .order_by(City.id)
        )
        stocks = (await db.execute(stock_stmt)).all()
        tree = await catalog_tree.get_tree(db)
        node = tree.by_id.get(product.category_id)
        path = tree.path(node) if node else []

    # Похожие товары: та же категория, сначала с той же моделью в названии
    related = []
    if product:
        related = (
            await db.execute(
                select(MobaPrice)
                .where(*_visible_filter(), MobaPrice.sku != sku,
                       exists().where(Product.sku == MobaPrice.sku, Product.category_id == product.category_id))
                .order_by(*_related_order(moba_price.name))
                .limit(8)
            )
        ).scalars().all()
    ctx = _card_context(request)
    related_stocks = await _stock_map(db, [r.sku for r in related], ctx["city"].id if ctx["city"] else None)

    return templates.TemplateResponse(
        request=request,
        name="product.html",
        context={
            "product": product,
            "moba_price": moba_price,
            "specs": _parse_specs(moba_price.specifications),
            "stocks": stocks,
            "sku": sku,
            "path": path,
            "related": related,
            "related_stocks": related_stocks,
            **ctx,
        },
    )
