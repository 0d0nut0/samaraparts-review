"""
Обмен с 1С:Управление торговлей 11 по CommerceML 2 (стандартный «Обмен с сайтами»).

1С сама обращается к сайту (/1c_exchange) и:
  - выгружает каталог: import.xml (группы, товары) и offers.xml (цены, остатки) → import_catalog / import_offers;
  - забирает новые заказы сайта (orders_xml) и подтверждает их получение (mark_exported).

Сопоставление:
  - товар: сначала по Ид 1С (products.onec_id), затем по артикулу (= sku сайта); новый товар создаётся
    в категории «Новые товары из 1С» — переложите его в нужный раздел в админке;
  - цены: принимается только ONEC_PRICE_TYPE_PURCHASE → закупочная цена;
    продажные цены рассчитываются на сайте из обычной розничной цены Moba;
  - остатки: склад 1С → город сайта по cities.onec_warehouse_id или совпадению названия;
    без разбивки по складам — в ONEC_DEFAULT_CITY_ID.
"""
import logging
import re
import uuid
import json
from pathlib import Path
import xml.etree.ElementTree as ET  # только для построения XML заказов
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation

from defusedxml.ElementTree import fromstring as safe_fromstring  # XML от 1С: защита от XXE/«billion laughs»
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models import Category, City, MobaPrice, Order, OrderItem, Product, Stock
from app.models.inventory_transaction import InventoryTransaction, TransactionType
from app.models.order import OrderStatus
from app.services import catalog_tree
from app.services.pricing import reference_prices, assign_prices

logger = logging.getLogger(__name__)

NEW_FROM_1C_SLUG = "novye-iz-1c"
CML_VERSION = "2.08"


@dataclass
class ImportResult:
    created: int = 0
    updated: int = 0
    prices: int = 0
    stocks: int = 0
    skipped: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return (f"создано {self.created}, обновлено {self.updated}, цен {self.prices}, остатков {self.stocks}"
                + (f", пропущено {len(self.skipped)}" if self.skipped else ""))


# ───────────────────────── XML-утилиты ─────────────────────────


def _strip_ns(root: ET.Element) -> ET.Element:
    """CommerceML бывает с пространством имён urn:1C.ru:commerceml_2 и без — приводим к именам без него."""
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def _text(el: ET.Element | None, path: str, default: str = "") -> str:
    found = el.find(path) if el is not None else None
    return (found.text or "").strip() if found is not None and found.text else default


def _decimal(value: str) -> Decimal | None:
    try:
        number = Decimal(value.replace(",", ".").replace("\xa0", "").replace(" ", "")) if value else None
        return number if number is not None and number.is_finite() and number >= 0 else None
    except InvalidOperation:
        return None


def _product_id(offer_id: str) -> str:
    """Ид предложения бывает «ИдТовара#ИдХарактеристики» — остатки и цены относим к товару."""
    return offer_id.split("#", 1)[0]


# ───────────────────────── Импорт каталога ─────────────────────────


async def _new_items_category(db: AsyncSession) -> Category:
    cat = (await db.execute(select(Category).where(Category.slug == NEW_FROM_1C_SLUG))).scalar_one_or_none()
    if not cat:
        cat = Category(name="Новые товары из 1С", slug=NEW_FROM_1C_SLUG, position=999)
        db.add(cat)
        await db.flush()
    return cat


async def _find_product(db: AsyncSession, onec_id: str, sku: str, barcode: str = "") -> Product | None:
    p = (await db.execute(select(Product).where(Product.onec_id == onec_id))).scalar_one_or_none()
    if p is None and sku:
        p = (await db.execute(select(Product).where(Product.sku == sku))).scalar_one_or_none()
    if p is None and barcode:
        p = (await db.execute(select(Product).where(Product.sku == barcode))).scalar_one_or_none()
    if p is not None and p.onec_id and p.onec_id != onec_id:
        raise ValueError(f"Товар {p.sku} уже связан с другим Ид 1С")
    return p


async def import_catalog(db: AsyncSession, xml_bytes: bytes) -> ImportResult:
    """import.xml: создаёт/обновляет товары. Коммит делает вызывающий код."""
    root = _strip_ns(safe_fromstring(xml_bytes))
    result = ImportResult()
    fallback_cat = None
    mapping_path = Path(settings.ONEC_CATALOG_MAP)
    name_to_sku = json.loads(mapping_path.read_text()) if mapping_path.is_file() else {}
    group_to_cat = {
        c.onec_id: c.id for c in (await db.execute(select(Category).where(Category.onec_id.isnot(None)))).scalars()
    }
    categories_by_slug = {c.slug:c.id for c in (await db.execute(select(Category))).scalars()}

    for item in root.iter("Товар"):
        onec_id = _text(item, "Ид")
        name = _text(item, "Наименование")
        barcode = _text(item, "Штрихкод")
        sku = _text(item, "Артикул") or barcode or name_to_sku.get(name.casefold()) or onec_id
        if not onec_id or not name or "#" in onec_id:
            result.skipped.append(onec_id or "?")
            continue
        if item.get("Статус") == "Удален" or _text(item, "ПометкаУдаления").lower() == "true":
            p = await _find_product(db, onec_id, sku)
            if p:
                p.is_active = 0
                result.updated += 1
            continue

        group_ids = [g.text.strip() for g in item.findall("Группы/Ид") if g.text]
        category_id = next((group_to_cat[g] for g in group_ids if g in group_to_cat), None)
        if category_id is None and settings.ONEC_SOURCE_ONLY:
            first_word = re.sub(r'[^a-zа-яё0-9]+', '', name.casefold().split()[0])
            category_id = categories_by_slug.get(first_word)

        product = await _find_product(db, onec_id, sku, barcode)
        mapped_sku = name_to_sku.get(name.casefold())
        if product is None and mapped_sku:
            product = await _find_product(db, onec_id, mapped_sku)
        if product is None:
            if category_id is None:
                fallback_cat = fallback_cat or await _new_items_category(db)
                category_id = fallback_cat.id
            slug = re.sub(r"[^a-z0-9]+", "-", sku.lower()).strip("-") or onec_id
            product = Product(sku=sku, name=name, slug=slug, category_id=category_id, is_active=1, onec_id=onec_id)
            db.add(product)
            result.created += 1
        else:
            product.onec_id = onec_id
            # The previous manual import used 1C's 100-character name field.
            # Keep our full name when the incoming name is just its truncation.
            if mapped_sku != product.sku and not (len(name) == 100 and product.name.startswith(name)):
                product.name = name
            if category_id:
                product.category_id = category_id
            result.updated += 1
        description = _text(item, "Описание")
        if description:
            product.description = description

        await db.flush()
        mp = (await db.execute(select(MobaPrice).where(MobaPrice.sku == product.sku))).scalars().first()
        if mp:
            mp.name = product.name
        else:
            # Витрина читает moba_prices; цена появится после offers.xml
            db.add(MobaPrice(sku=product.sku, name=name, price=0))
        await db.flush()

    catalog_tree.invalidate()
    return result


async def import_offers(db: AsyncSession, xml_bytes: bytes) -> ImportResult:
    """offers.xml: цены и остатки по складам. Коммит делает вызывающий код."""
    root = _strip_ns(safe_fromstring(xml_bytes))
    result = ImportResult()

    price_types = {_text(t, "Ид"): _text(t, "Наименование") for t in root.iter("ТипЦены")}
    cost_type = next((i for i, n in price_types.items() if n == settings.ONEC_PRICE_TYPE_PURCHASE), None)

    cities = (await db.execute(select(City))).scalars().all()
    warehouses = {_text(w, "Ид"): _text(w, "Наименование") for w in root.iter("Склад") if _text(w, "Ид")}
    city_by_wh: dict[str, int] = {}
    for wh_id, wh_name in warehouses.items():
        city = next((c for c in cities if c.onec_warehouse_id == wh_id), None) or next(
            (c for c in cities if c.name.lower() in wh_name.lower()), None
        )
        if city:
            city_by_wh[wh_id] = city.id
    for c in cities:
        if c.onec_warehouse_id:
            city_by_wh.setdefault(c.onec_warehouse_id, c.id)

    for offer in root.iter("Предложение"):
        raw_id = _text(offer, "Ид")
        if "#" in raw_id:
            result.skipped.append(raw_id)
            continue  # характеристики нельзя молча смешивать с базовым товаром
        onec_id = _product_id(raw_id)
        product = await _find_product(db, onec_id, _text(offer, "Артикул"))
        if not product:
            result.skipped.append(onec_id)
            continue

        prices = {}
        for price in offer.findall("Цены/Цена"):
            value = _decimal(_text(price, "ЦенаЗаЕдиницу"))
            if value is not None:
                prices[_text(price, "ИдТипаЦены")] = value
        cost = prices.get(cost_type) if cost_type else None
        if cost is not None and cost > 0:
            product.purchase_price = cost
            mp = (await db.execute(select(MobaPrice).where(MobaPrice.sku == product.sku))).scalars().first()
            if mp and mp.price > 0 and (mp.url or mp.our_opt):
                assign_prices(mp, product, reference_prices(mp.price, purchase_price=cost))
            result.prices += 1

        if not settings.ONEC_IMPORT_STOCKS:
            await db.flush()
            continue
        # Остатки: <Склад ИдСклада=".." КоличествоНаСкладе=".."/> или <Остатки><Остаток><Склад><Ид/><Количество/>
        per_city: dict[int, int] = {}
        for wh in offer.findall("Склад"):
            city_id = city_by_wh.get(wh.get("ИдСклада", ""))
            qty = _decimal(wh.get("КоличествоНаСкладе", ""))
            if city_id and qty is not None and qty == qty.to_integral_value():
                per_city[city_id] = per_city.get(city_id, 0) + int(qty)
        for rest in ([] if per_city else offer.findall("Остатки/Остаток/Склад")):
            city_id = city_by_wh.get(_text(rest, "Ид"))
            qty = _decimal(_text(rest, "Количество"))
            if city_id and qty is not None and qty == qty.to_integral_value():
                per_city[city_id] = per_city.get(city_id, 0) + int(qty)
        if not per_city and settings.ONEC_DEFAULT_CITY_ID:
            qty = _decimal(_text(offer, "Количество"))
            if qty is not None and qty == qty.to_integral_value():
                per_city[settings.ONEC_DEFAULT_CITY_ID] = int(qty)

        for city_id, qty in per_city.items():
            qty = max(qty, 0)
            stock = (
                await db.execute(select(Stock).where(Stock.product_id == product.id, Stock.city_id == city_id))
            ).scalar_one_or_none()
            old = stock.quantity if stock else 0
            if stock:
                stock.quantity = qty
            else:
                db.add(Stock(product_id=product.id, city_id=city_id, quantity=qty))
            if qty != old:
                db.add(InventoryTransaction(
                    product_id=product.id, type=TransactionType.ADJUSTMENT, quantity=qty - old,
                    to_city_id=city_id if qty > old else None, from_city_id=city_id if qty < old else None,
                    notes="Остаток из 1С",
                ))
            result.stocks += 1
        await db.flush()

    catalog_tree.invalidate()
    return result


# ───────────────────────── Выгрузка заказов ─────────────────────────


async def pending_orders(db: AsyncSession) -> list[Order]:
    """Заказы, которые 1С ещё не забрала (кроме отменённых до выгрузки)."""
    return (
        await db.execute(
            select(Order)
            .options(selectinload(Order.items).selectinload(OrderItem.product), selectinload(Order.city))
            .where(Order.onec_exported_at.is_(None), Order.status != OrderStatus.CANCELLED)
            .order_by(Order.id)
        )
    ).scalars().all()


def orders_xml(orders: list[Order]) -> bytes:
    """КоммерческаяИнформация с документами «Заказ товара» (CommerceML 2.08)."""
    now = datetime.now()
    root = ET.Element("КоммерческаяИнформация", {"ВерсияСхемы": CML_VERSION, "ДатаФормирования": now.strftime("%Y-%m-%dT%H:%M:%S")})
    for o in orders:
        doc = ET.SubElement(root, "Документ")
        for tag, value in (
            ("Ид", f"site-order-{o.id}"),
            ("Номер", str(o.id)),
            ("Дата", o.created_at.strftime("%Y-%m-%d")),
            ("Время", o.created_at.strftime("%H:%M:%S")),
            ("ХозОперация", "Заказ товара"),
            ("Роль", "Продавец"),
            ("Валюта", "руб"),
            ("Курс", "1"),
            ("Сумма", f"{o.total_amount:.2f}"),
            ("Комментарий", f"Заказ с сайта #{o.id}. Город: {o.city.name if o.city else '—'}. Адрес: {o.address or 'самовывоз'}"),
        ):
            ET.SubElement(doc, tag).text = value
        agents = ET.SubElement(ET.SubElement(doc, "Контрагенты"), "Контрагент")
        ET.SubElement(agents, "Ид").text = f"site-user-{o.user_id or 'guest'}"
        ET.SubElement(agents, "Наименование").text = o.customer_name
        ET.SubElement(agents, "ПолноеНаименование").text = o.customer_name
        ET.SubElement(agents, "Роль").text = "Покупатель"
        contact = ET.SubElement(ET.SubElement(agents, "Контакты"), "Контакт")
        ET.SubElement(contact, "Тип").text = "Телефон рабочий"
        ET.SubElement(contact, "Значение").text = o.customer_phone

        goods = ET.SubElement(doc, "Товары")
        for it in o.items:
            g = ET.SubElement(goods, "Товар")
            p = it.product
            ET.SubElement(g, "Ид").text = (p.onec_id if p and p.onec_id else (p.sku if p else str(it.product_id)))
            ET.SubElement(g, "Артикул").text = p.sku if p else ""
            ET.SubElement(g, "Наименование").text = p.name if p else f"Товар #{it.product_id}"
            ET.SubElement(g, "БазоваяЕдиница", {"Код": "796", "НаименованиеПолное": "Штука"}).text = "шт"
            ET.SubElement(g, "ЦенаЗаЕдиницу").text = f"{it.price_at_time:.2f}"
            ET.SubElement(g, "Количество").text = str(it.quantity)
            ET.SubElement(g, "Сумма").text = f"{it.price_at_time * it.quantity:.2f}"
            props = ET.SubElement(ET.SubElement(g, "ЗначенияРеквизитов"), "ЗначениеРеквизита")
            ET.SubElement(props, "Наименование").text = "ТипНоменклатуры"
            ET.SubElement(props, "Значение").text = "Товар"

        reqs = ET.SubElement(doc, "ЗначенияРеквизитов")
        for name, value in (("Статус заказа", o.status.name), ("Тип заказа", o.order_type.name)):
            r = ET.SubElement(reqs, "ЗначениеРеквизита")
            ET.SubElement(r, "Наименование").text = name
            ET.SubElement(r, "Значение").text = value
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8")


async def mark_exported(db: AsyncSession, order_ids: list[int]) -> None:
    if not order_ids:
        return
    for o in (await db.execute(select(Order).where(Order.id.in_(order_ids)))).scalars():
        o.onec_exported_at = datetime.now()


def new_session_token() -> str:
    return uuid.uuid4().hex


async def price_feed(db: AsyncSession) -> dict:
    """Рассчитанные цены для обратной загрузки. Без заказов и персональных данных."""
    rows = (await db.execute(select(Product, MobaPrice).join(MobaPrice, Product.sku == MobaPrice.sku)
                            .where(Product.is_active == 1, MobaPrice.our_base.isnot(None))
                            .order_by(Product.id))).all()
    items = []
    for p, mp in rows:
        items.append(dict(id=p.onec_id, sku=p.sku, name=p.name,
                          retail=int(mp.our_base) if mp.our_base else None,
                          wholesale=int(mp.our_opt) if mp.our_opt else None,
                          super_wholesale=int(mp.our_super_opt) if mp.our_super_opt else None,
                          super_status='ready' if mp.our_super_opt else 'purchase_price_required',
                          parsed_at=mp.parsed_at.isoformat() if mp.parsed_at else None))
    return dict(version=1, currency='RUB', price_types=['Розница', 'ОПТ', 'СуперОПТ'], items=items)
