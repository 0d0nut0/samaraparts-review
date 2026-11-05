"""
Импорт поставки на сайт: создаёт/обновляет товары, цены и остатки.

Источники (любой можно не указывать):
  --glass  CSV накладной стёкол REMAX (data/imports/remax_glass_*.csv): line;series;privacy;model;qty;cost
  --order  «Заказ клиента …xlsx» (1С): №, Артикул, Товар, Количество, ед., Цена, Сумма, Штрихкод
  --supply «Поставка …xlsx»: Штрихкод, Номенклатура, Model, Quality, PCS (без цен)

Цены поставки сохраняются только как закупка. Продажные цены рассчитываются по Moba
через run_parser.py. Тип цен из клиентского заказа подтверждается флагом --prices-are-costs.

Повторный запуск безопасен: приход каждой позиции записывается один раз (по примечанию «Импорт: …»).

Пример:
  python -m scripts.import_supply --glass data/imports/remax_glass_2026-10-05.csv \
      --order "Заказ клиента № 2444 от 06.10.2026.xlsx" --supply "Поставка Партс октябрь.xlsx" --city 1
"""
import argparse
import csv
import math
import re
from dataclasses import dataclass
from decimal import Decimal

import openpyxl
from sqlalchemy import select

from app.database import SessionLocal
from app.models import Category, City, InventoryTransaction, MobaPrice, Product, Stock, SystemConfig
from app.models.inventory_transaction import TransactionType


@dataclass
class Item:
    sku: str
    name: str
    category: str  # slug
    qty: int
    cost: Decimal | None
    source: str


# ───────────────────────── Разбор источников ─────────────────────────


def read_glass(path: str) -> list[Item]:
    items = []
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f, delimiter=";"):
            privacy = row["privacy"] == "1"
            kind = "Privacy (антишпион) " if privacy else ""
            name = f"Защитное стекло REMAX {row['series']} {kind}для {row['model']}"
            model_code = re.sub(r"[^0-9a-z]+", "", row["model"].lower().replace("iphone", "ip"))
            sku = f"RMX-{row['series']}{'-PRV' if privacy else ''}-{model_code}".upper()
            items.append(Item(sku, name, "zashchitnye-stekla-remax", int(row["qty"]), Decimal(row["cost"]), "REMAX 05.10.2026"))
    return items


ACCESSORY_RULES = [
    (r"^(азу|сзу)\b", "zaryadnye-ustroystva"),
    (r"^внешний аккумулятор", "vneshnie-akkumulyatory-power-bank"),
    (r"^держатель", "derzhateli"),
    (r"^(кабель|адаптер|usb-c-хаб)", "kabeli-adaptery"),
    (r"^(стереогарнитура|гарнитура)", "garnitury"),
    (r"наушники", "besprovodnye-naushniki-bluetooth"),
    (r"^защитные линзы", "zashchitnye-stekla-dlya-kamery"),
]


def read_order(path: str) -> list[Item]:
    ws = openpyxl.load_workbook(path, data_only=True).active
    items, header_seen = [], False
    for row in ws.iter_rows(values_only=True):
        cells = [c for c in row if c not in (None, "")]
        if not cells:
            continue
        if cells[0] == "№":
            header_seen = True
            continue
        if not header_seen or not isinstance(cells[0], (int, float)) or len(cells) < 6:
            continue
        _, article, name, qty, _unit, price = cells[:6]
        low = str(name).lower()
        category = next((slug for pattern, slug in ACCESSORY_RULES if re.search(pattern, low)), "aksessuary")
        items.append(Item(f"BRK-{article}", str(name).strip(), category, int(qty), Decimal(str(price)), "Заказ 2444"))
    return items


def _part_category(name: str) -> str:
    low = name.lower()
    if low.startswith(("дисплей", "дисплейный модуль")):
        return "displei"
    if low.startswith("аккумулятор"):
        return "akkumulyatory-dlya-mobilnykh-ustroystv"
    if low.startswith(("задняя крышка", "корпус")):
        return "zadnie-kryshki-i-korpusa"
    if low.startswith("шлейф"):
        return "shleyfy-platy"
    return "zapchasti"


def read_supply(path: str) -> list[Item]:
    ws = openpyxl.load_workbook(path, data_only=True).active
    merged: dict[str, Item] = {}
    for row in ws.iter_rows(min_row=2, values_only=True):
        barcode, name, _model, _quality, qty = (list(row) + [None] * 5)[:5]
        if not barcode or not name or not qty:
            continue
        sku = f"PS-{barcode}"
        if sku in merged:
            merged[sku].qty += int(qty)
        else:
            merged[sku] = Item(sku, str(name).strip(), _part_category(str(name)), int(qty), None, "Поставка октябрь")
    return list(merged.values())


# ───────────────────────── Запись в БД ─────────────────────────


def apply(items: list[Item], city_id: int) -> dict:
    stats = {"created": 0, "updated": 0, "received": 0, "skipped_receipts": 0, "no_price": 0}
    with SessionLocal() as db:
        cats = {c.slug: c.id for c in db.execute(select(Category)).scalars()}
        if not db.get(City, city_id):
            raise SystemExit(f"Город {city_id} не найден")

        for it in items:
            product = db.execute(select(Product).where(Product.sku == it.sku)).scalar_one_or_none()
            if product is None:
                product = Product(sku=it.sku, name=it.name, slug=it.sku.lower(), category_id=cats.get(it.category, cats["zapchasti"]),
                                  is_active=1, critical_stock_threshold=2)
                db.add(product)
                stats["created"] += 1
            else:
                stats["updated"] += 1
            product.name = it.name
            if it.cost is not None:
                product.purchase_price = it.cost
            db.flush()

            mp = db.execute(select(MobaPrice).where(MobaPrice.sku == it.sku)).scalars().first()
            if mp is None:
                mp = MobaPrice(sku=it.sku, name=it.name, price=0)
                db.add(mp)
            mp.name = it.name
            if it.cost is not None and mp.url and mp.price > 0:
                from app.services.pricing import reference_prices, assign_prices
                assign_prices(mp, product, reference_prices(mp.price, purchase_price=it.cost))
            if mp.our_base is None:
                stats['no_price'] += 1

            note = f"Импорт: {it.source} {it.sku}"
            if db.execute(select(InventoryTransaction.id).where(InventoryTransaction.notes == note)).first():
                stats["skipped_receipts"] += 1
                continue
            stock = db.execute(select(Stock).where(Stock.product_id == product.id, Stock.city_id == city_id)).scalar_one_or_none()
            if stock:
                stock.quantity += it.qty
            else:
                db.add(Stock(product_id=product.id, city_id=city_id, quantity=it.qty))
            db.add(InventoryTransaction(product_id=product.id, type=TransactionType.RECEIPT, quantity=it.qty,
                                        to_city_id=city_id, unit_cost=it.cost, notes=note))
            stats["received"] += it.qty
        db.commit()
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--glass")
    ap.add_argument("--order")
    ap.add_argument("--supply")
    ap.add_argument("--city", type=int, required=True, help="Явно выбранный склад (cities.id)")
    ap.add_argument("--prices-are-costs", action="store_true", help="Подтверждено: цены --order являются закупочными")
    args = ap.parse_args()
    if args.order and not args.prices_are_costs:
        ap.error("Подтвердите, что цены заказа — закупка: --prices-are-costs")

    items: list[Item] = []
    for reader, path in ((read_glass, args.glass), (read_order, args.order), (read_supply, args.supply)):
        if path:
            part = reader(path)
            print(f"{path}: {len(part)} позиций, {sum(i.qty for i in part)} шт.")
            items += part
    print(apply(items, args.city))


if __name__ == "__main__":
    main()
