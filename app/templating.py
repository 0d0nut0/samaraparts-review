"""
Общий экземпляр Jinja2Templates и подписи для шаблонов (статусы, роли).
Все роутеры импортируют templates отсюда, чтобы глобальные переменные были одинаковыми.
"""
from pathlib import Path

from fastapi.templating import Jinja2Templates

from app import legal, seo
from app.services.pricing import buyer_prices
from app.shop_info import SHOP, contact_for_city

BASE_DIR = Path(__file__).resolve().parent

ORDER_STATUS_LABELS = {
    "PENDING": "Новый",
    "PAID": "Оплачен",
    "COMPLETED": "Выполнен",
    "CANCELLED": "Отменён",
}

ORDER_STATUS_CLASSES = {
    "PENDING": "bg-yellow-500/10 text-yellow-600 dark:text-yellow-400 border-yellow-500/30",
    "PAID": "bg-blue-500/10 text-blue-600 dark:text-blue-400 border-blue-500/30",
    "COMPLETED": "bg-green-500/10 text-green-600 dark:text-green-400 border-green-500/30",
    "CANCELLED": "bg-red-500/10 text-red-600 dark:text-red-400 border-red-500/30",
}

ROLE_LABELS = {
    "ADMIN": "Администратор",
    "WHOLESALE": "Оптовый покупатель",
    "RETAIL": "Покупатель",
}

TRANSACTION_LABELS = {
    "RECEIPT": "Приход",
    "WRITE_OFF": "Списание",
    "TRANSFER": "Перемещение",
    "SALE": "Продажа",
    "RETURN": "Возврат",
    "ADJUSTMENT": "Корректировка",
}

APPLICATION_STATUS_LABELS = {
    "PENDING": "На рассмотрении",
    "APPROVED": "Одобрена",
    "REJECTED": "Отклонена",
}

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals.update(
    status_labels=ORDER_STATUS_LABELS,
    status_classes=ORDER_STATUS_CLASSES,
    role_labels=ROLE_LABELS,
    transaction_labels=TRANSACTION_LABELS,
    application_labels=APPLICATION_STATUS_LABELS,
    shop=SHOP,
    shop_contact=contact_for_city,
    buyer_prices=buyer_prices,
    seo=seo,
    legal=legal,
)
