"""Вытаскивает полный список категорий с moba.ru и сохраняет в JSON."""
import asyncio
import json
import re

from playwright.async_api import async_playwright


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto("https://moba.ru", wait_until="networkidle", timeout=60000)

        html = await page.content()
        await browser.close()

    # Ищем popupCatalog([...])
    match = re.search(r"popupCatalog\(\s*(\[.*?\])\s*\)", html, re.DOTALL)
    if not match:
        print("Не нашли popupCatalog в HTML")
        return

    raw = match.group(1)

    # Находим границы массива (учитываем вложенность)
    depth = 0
    end = 0
    for i, ch in enumerate(raw):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    data = json.loads(raw[:end])

    # Собираем все ссылки на категории (только те, что ведут в /catalog/)
    all_categories = []

    def walk(items, level=0):
        for item in items:
            link = item.get("linkUrl", "")
            name = item.get("name", "")
            if "/catalog/" in link and not link.endswith("/catalog/"):
                all_categories.append({"name": name, "url": link, "level": level})
            children = item.get("children", [])
            if children:
                walk(children, level + 1)

    walk(data)

    # Сохраняем в файл
    with open("categories.json", "w", encoding="utf-8") as f:
        json.dump(all_categories, f, ensure_ascii=False, indent=2)

    print(f"Найдено категорий: {len(all_categories)}\n")
    for c in all_categories[:30]:
        indent = "  " * c["level"]
        print(f"{indent}{c['url']}  ← {c['name']}")

    if len(all_categories) > 30:
        print(f"\n... и ещё {len(all_categories) - 30} категорий")
    print(f"\nПолный список: categories.json")


asyncio.run(main())
