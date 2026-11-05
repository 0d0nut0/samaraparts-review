"""Общее для обработчиков бота: сессия БД и аккаунт в каждом обновлении, клавиатуры, форматирование."""
import html
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, TelegramObject, WebAppInfo,
)

from app.config import settings
from app.database import AsyncSessionLocal
from app.services import accounts
from app.services.telegram import money, site  # noqa: F401  (реэкспорт для обработчиков)

BTN_CATALOG = "🛍 Каталог"
BTN_SEARCH = "🔍 Поиск"
BTN_CART = "🛒 Корзина"
BTN_ORDERS = "📦 Мои заказы"
BTN_CHAT = "💬 Менеджер"
BTN_PROFILE = "👤 Профиль"
BTN_ADMIN = "⚙️ Админка"


class AccountMiddleware(BaseMiddleware):
    """Каждому обновлению — своя сессия БД и аккаунт сайта этого Telegram (создаётся при первом обращении)."""

    async def __call__(self, handler: Callable[[TelegramObject, dict], Awaitable[Any]], event: TelegramObject, data: dict) -> Any:
        tg = data.get("event_from_user")
        async with AsyncSessionLocal() as db:
            data["db"] = db
            if tg and not tg.is_bot:
                data["user"] = await accounts.telegram_user(db, tg.id, tg.full_name, tg.username or "")
            return await handler(event, data)


def webapp_url(path: str = "/tg/app") -> str | None:
    """Mini App открывается только по https (требование Telegram)."""
    return site(path) if settings.SITE_URL.startswith("https://") else None


def main_menu(user) -> ReplyKeyboardMarkup:
    url = webapp_url()
    catalog = KeyboardButton(text=BTN_CATALOG, web_app=WebAppInfo(url=url)) if url else KeyboardButton(text=BTN_CATALOG)
    rows = [
        [catalog, KeyboardButton(text=BTN_SEARCH)],
        [KeyboardButton(text=BTN_CART), KeyboardButton(text=BTN_ORDERS)],
        [KeyboardButton(text=BTN_CHAT), KeyboardButton(text=BTN_PROFILE)],
    ]
    if user is not None and user.is_superuser:
        rows.append([KeyboardButton(text=BTN_ADMIN)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, input_field_placeholder="Модель и деталь: «iPhone 11 дисплей»")


def kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup | None:
    """[[(текст, callback | "url:…")]] → inline-клавиатура (ссылки не на https Telegram не принимает — пропускаем)."""
    from app.services.telegram import keyboard
    return keyboard(rows)


def esc(text) -> str:
    return html.escape(str(text or ""))
