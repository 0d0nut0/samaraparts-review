"""
Сведения о продавце для страниц сайта и подвала.

На сайте показываем только то, что нужно покупателю и требует закон (ФИО ИП, ИНН, ОГРНИП, контакты).
Банковские реквизиты нужны для счёта юрлицу — их выставляет менеджер, публиковать не требуется.
"""

from app.config import settings

SHOP = {
    "brand": "ПАРТС",
    "seller": "Индивидуальный предприниматель Герасимов Александр Александрович",
    "seller_short": "ИП Герасимов А. А.",
    "inn": "631219478194",
    "ogrnip": "326632700163361",
    # Самарский городской номер сохранён ровно в формате заказчика.
    "phone": settings.SAMARA_PHONE or "8997040204",
    "phone_href": "tel:" + (settings.SAMARA_PHONE or "8997040204").replace(" ", ""),
    "branches": [
        {"name": "Самара", "phone": settings.SAMARA_PHONE or "8997040204",
         "phone_href": "tel:" + (settings.SAMARA_PHONE or "8997040204").replace(" ", ""),
         "address": None, "hours": None, "coming_soon": False},
        {"name": "Казань", "phone": settings.KAZAN_PHONE or None,
         "phone_href": "tel:" + settings.KAZAN_PHONE.replace(" ", "") if settings.KAZAN_PHONE else None,
         "address": "г. Казань, проспект Победы, 139к2", "hours": None, "coming_soon": True},
    ],
    "cities": ["Казань", "Самара"],
    "cities_in": "Казани и Самаре",  # «в Казани и Самаре» — для текстов страниц
    # Пункт выдачи и часы — из настроек Telegram-бота (WAREHOUSE_ADDRESS / WAREHOUSE_HOURS)
    "address": None,
    "hours": None,
    "email": None,
}


def contact_for_city(city=None):
    name = getattr(city, "name", "Самара")
    branch = next((b for b in SHOP["branches"] if b["name"].casefold() == name.casefold()), SHOP["branches"][0])
    return {"phone": branch["phone"] or "контакты магазина", "phone_href": branch["phone_href"] or "/contacts"}
