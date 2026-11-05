import json
"""
Парсер цен с moba.ru.

Стратегия:
1. Заходим на страницу категории через Playwright (браузер нужен, т.к. сайт на JS)
2. Из inline-скриптов вытаскиваем JSON-массив товаров
3. Идём по страницам ?page=1, 2, 3... пока есть товары
4. Сохраняем в таблицу moba_prices

Запуск вручную:
    python -m app.services.moba_parser

Или из кода:
    asyncio.run(parse_all_categories())
"""
import asyncio
import html as html_module
import json
import re
from decimal import Decimal

from playwright.async_api import async_playwright
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import MobaPrice


MOBA_BASE = "https://moba.ru"

import json
from pathlib import Path


def load_categories() -> list[str]:
    """Загружает список категорий из categories.json (создаётся extract_categories.py)."""
    path = Path("categories.json")
    if not path.exists():
        raise FileNotFoundError(
            "categories.json не найден. Сначала запусти: python extract_categories.py"
        )
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return [c["url"] for c in data]


# Список подгружается из categories.json при запуске
CATEGORIES: list[str] = []


def refresh_categories() -> None:
    """Обновляет глобальный список категорий."""
    global CATEGORIES
    CATEGORIES = load_categories()

def extract_products_from_html(html: str) -> list[dict]:
    """Читает публичные товары из JSON, в том числе из HTML-атрибута x-data.

    На текущем сайте массив находится в атрибуте с &quot;, а не только
    в script. JSONDecoder учитывает скобки внутри названий и вложенных полей;
    никакой JavaScript из страницы не выполняется.
    """
    source = html_module.unescape(html)
    decoder = json.JSONDecoder()
    result = {}
    for match in re.finditer(r'\[\s*\{\s*"id"\s*:', source):
        try:
            items, _ = decoder.raw_decode(source, match.start())
        except (ValueError, json.JSONDecodeError):
            continue
        if not isinstance(items, list) or not items:
            continue
        if not all(isinstance(p, dict) and {"article", "name", "price"} <= p.keys() for p in items):
            continue
        for item in items:
            if item.get("article"):
                result.setdefault(item["article"], item)
    return list(result.values())


def extract_category_tree(html: str) -> list[dict]:
    """Публичное дерево категорий из popupCatalog; сохраняет вложенность."""
    source = html_module.unescape(html)
    match = re.search(r"popupCatalog\(\s*(?=\[)", source)
    if not match:
        return []
    try:
        tree, _ = json.JSONDecoder().raw_decode(source, match.end())
    except (ValueError, json.JSONDecodeError):
        return []
    return tree if isinstance(tree, list) else []


def category_page_count(html: str, category_path: str) -> int:
    """Использует номера из настоящих ссылок пагинации этой категории."""
    from urllib.parse import parse_qs, urlsplit
    source = html_module.unescape(html)
    expected = urlsplit(category_path).path.rstrip("/")
    pages = [1]
    for href in re.findall(r'''href=["']([^"']+)["']''', source):
        parts = urlsplit(href)
        if parts.netloc and parts.netloc not in {"moba.ru", "www.moba.ru"}:
            continue
        if parts.path.rstrip("/") != expected:
            continue
        for value in parse_qs(parts.query).get("page", []):
            if value.isdigit() and 0 < int(value) <= 2000:
                pages.append(int(value))
    return max(pages)


async def fetch_page_html(page, url: str) -> str:
    """Открывает URL и возвращает HTML."""
    await page.goto(url, wait_until="networkidle", timeout=60000)
    return await page.content()


async def parse_category(page, category_path: str) -> list[dict]:
    """
    Парсит одну категорию целиком, обходя все страницы пагинации.
    Возвращает список товаров с добавленным полем url.
    """
    all_products: list[dict] = []
    seen_articles: set[str] = set()
    page_num = 1

    while True:
        if page_num == 1:
            url = f"{MOBA_BASE}{category_path}"
        else:
            url = f"{MOBA_BASE}{category_path}?page={page_num}"

        print(f"  → {url}")

        try:
            html = await fetch_page_html(page, url)
        except Exception as e:
            print(f"     ошибка загрузки: {e}")
            break

        products = extract_products_from_html(html)

        if not products:
            print(f"     товаров нет — стоп")
            break

        # Добавляем url к каждому товару
        for p in products:
            p["_parsed_url"] = url

        # Дедупликация внутри категории (одна и та же карточка может попасть на несколько страниц)
        new_products = []
        for p in products:
            article = p.get("article") or ""
            if article and article in seen_articles:
                continue
            if article:
                seen_articles.add(article)
            new_products.append(p)

        if not new_products:
            print(f"     все товары уже видели — стоп")
            break

        print(f"     найдено товаров: {len(new_products)}")
        all_products.extend(new_products)

        page_num += 1

        # Защита от бесконечного цикла
        if page_num > 100:
            print("     превышен лимит страниц (100)")
            break

        # Небольшая пауза между запросами, чтобы не злить moba
        await asyncio.sleep(3)

    return all_products


def save_products(db: Session, products: list[dict]) -> int:
    raise RuntimeError('Используйте run_parser.py --apply: он сопоставляет наш каталог и применяет единую политику цен')


async def parse_all_categories() -> None:
    raise RuntimeError('Используйте python run_parser.py --apply для полного проверенного снимка Moba')


if __name__ == '__main__':
    from run_parser import main
    main()
