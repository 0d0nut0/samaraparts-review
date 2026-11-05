"""
SEO: robots и canonical для каждой страницы, sitemap.xml, robots.txt, разметка schema.org.

Индексируем каталог, разделы, карточки товаров и информационные страницы.
Не индексируем поиск (?q=), корзину, оформление, вход/кабинет, админку, чат и служебные адреса —
у них <meta name="robots" content="noindex"> и Disallow в robots.txt.
"""
import json
from datetime import date
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse, Response
from markupsafe import Markup
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import MobaPrice, Product
from app.shop_info import SHOP

router = APIRouter(tags=["seo"])

PRIVATE = ("/admin", "/auth", "/cart", "/checkout", "/chat", "/tg", "/city", "/1c_exchange", "/health")
SITEMAP_CHUNK = 10000
PAGES = ["/catalog", "/about", "/contacts", "/delivery", "/payment", "/warranty", "/privacy"]


def absolute(path: str) -> str:
    return settings.SITE_URL.rstrip("/") + path


def robots(request: Request) -> str:
    path = request.url.path
    if path.startswith(PRIVATE) or request.query_params.get("q"):
        return "noindex, follow"
    return "index, follow"


def canonical(request: Request) -> str:
    """Адрес без лишних параметров: раздел каталога и номер страницы сохраняем, остальное отбрасываем."""
    keep = [(k, v) for k, v in request.query_params.items() if k in ("cat",) or (k == "page" and v not in ("", "1"))]
    query = "&".join(f"{k}={v}" for k, v in keep)
    return absolute(request.url.path) + (f"?{query}" if query else "")


def bot_link() -> str:
    from app.services.telegram import bot_link as link
    return link()


def jsonld(data: dict) -> Markup:
    body = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return Markup(f'<script type="application/ld+json">{body}</script>')


def organization() -> Markup:
    data = {
        "@context": "https://schema.org",
        "@type": "Store",
        "name": SHOP["brand"],
        "url": absolute("/catalog"),
        "logo": absolute("/static/favicon.svg"),
        "telephone": SHOP["phone"],
        "contactPoint": [{"@type": "ContactPoint", "telephone": b["phone"], "areaServed": b["name"],
                          "contactType": "customer service"} for b in SHOP["branches"] if b["phone"]],
        "legalName": SHOP["seller"],
        "taxID": SHOP["inn"],
        "areaServed": SHOP["cities"],
    }
    if SHOP.get("address"):
        data["address"] = {"@type": "PostalAddress", "streetAddress": SHOP["address"], "addressCountry": "RU"}
    if SHOP.get("hours"):
        data["openingHours"] = "Mo-Su 10:00-21:00"
    return jsonld(data)


def product(mp, price, in_stock: bool, path: list, image: str | None) -> Markup:
    data = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": mp.name,
        "sku": mp.sku,
        "url": absolute(f"/catalog/{mp.sku}"),
    }
    if image:
        data["image"] = image if image.startswith("http") else absolute(image)
    if path:
        data["category"] = " / ".join(n.name for n in path)
    if price is not None:
        data["offers"] = {
            "@type": "Offer",
            "price": f"{price:.2f}",
            "priceCurrency": "RUB",
            "availability": "https://schema.org/InStock" if in_stock else "https://schema.org/OutOfStock",
            "url": absolute(f"/catalog/{mp.sku}"),
            "seller": {"@type": "Organization", "name": SHOP["brand"]},
        }
    return jsonld(data)


def crumbs(path: list) -> list[tuple[str, str]]:
    """Узлы дерева каталога → [(название, адрес)] для хлебных крошек, начиная с «Каталог»."""
    return [("Каталог", "/catalog")] + [(n.name, f"/catalog?cat={n.slug}") for n in path]


def breadcrumbs(items: list[tuple[str, str]]) -> Markup:
    return jsonld({
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": i, "name": name, "item": absolute(url)}
            for i, (name, url) in enumerate(items, 1)
        ],
    })


# ───────────────────────── robots.txt и sitemap ─────────────────────────


@router.get("/robots.txt", response_class=PlainTextResponse, include_in_schema=False)
def robots_txt() -> str:
    lines = ["User-agent: *"] + [f"Disallow: {p}" for p in PRIVATE] + [
        "Disallow: /*?q=",
        "Disallow: /*&q=",
        "Clean-param: q&page /catalog",
        "",
        f"Sitemap: {absolute('/sitemap.xml')}",
    ]
    return "\n".join(lines) + "\n"


def _xml(body: str) -> Response:
    return Response('<?xml version="1.0" encoding="UTF-8"?>\n' + body, media_type="application/xml")


def _visible():
    return ~exists().where(Product.sku == MobaPrice.sku, Product.is_active == 0)


@router.get("/sitemap.xml", include_in_schema=False)
async def sitemap_index(db: AsyncSession = Depends(get_db)) -> Response:
    total = (await db.execute(select(func.count(MobaPrice.id)).where(_visible()))).scalar_one()
    today = date.today().isoformat()
    parts = ["/sitemap-pages.xml"] + [f"/sitemap-products-{i + 1}.xml" for i in range((total + SITEMAP_CHUNK - 1) // SITEMAP_CHUNK)]
    items = "".join(f"<sitemap><loc>{escape(absolute(p))}</loc><lastmod>{today}</lastmod></sitemap>" for p in parts)
    return _xml(f'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{items}</sitemapindex>')


@router.get("/sitemap-pages.xml", include_in_schema=False)
async def sitemap_pages(db: AsyncSession = Depends(get_db)) -> Response:
    from app.services import catalog_tree
    tree = await catalog_tree.get_tree(db)
    urls = [absolute(p) for p in PAGES] + [absolute(f"/catalog?cat={n.slug}") for n in tree.by_slug.values()]
    items = "".join(f"<url><loc>{escape(u)}</loc></url>" for u in urls)
    return _xml(f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{items}</urlset>')


@router.get("/sitemap-products-{n}.xml", include_in_schema=False)
async def sitemap_products(n: int, db: AsyncSession = Depends(get_db)) -> Response:
    rows = (
        await db.execute(
            select(MobaPrice.sku, MobaPrice.parsed_at).where(_visible()).order_by(MobaPrice.id)
            .offset((max(n, 1) - 1) * SITEMAP_CHUNK).limit(SITEMAP_CHUNK)
        )
    ).all()
    items = "".join(
        f"<url><loc>{escape(absolute('/catalog/' + sku))}</loc>"
        + (f"<lastmod>{ts.date().isoformat()}</lastmod>" if ts else "") + "</url>"
        for sku, ts in rows
    )
    return _xml(f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{items}</urlset>')
