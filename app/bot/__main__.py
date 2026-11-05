"""
Telegram-бот Parts.Site на базе сайта: python -m app.bot

Нужны TELEGRAM_BOT_TOKEN, TELEGRAM_BOT_USERNAME и SITE_URL (https — для Mini App) в .env,
та же база и Redis, что у сайта. Запускается одним процессом рядом с сайтом (docker-compose: сервис bot).
"""
import asyncio
import logging
import sys

from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import BotCommand, MenuButtonWebApp, WebAppInfo

from app.bot import shop, staff
from app.bot.common import AccountMiddleware, webapp_url
from app.config import settings
from app.services import telegram

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("bot")


def build_dispatcher(storage=None) -> Dispatcher:
    dp = Dispatcher(storage=storage or MemoryStorage())
    dp.update.outer_middleware(AccountMiddleware())
    dp.include_router(staff.router)  # действия администратора раньше «текст = поиск»
    dp.include_router(shop.router)
    return dp


async def main() -> None:
    if not settings.TELEGRAM_BOT_TOKEN:
        sys.exit("TELEGRAM_BOT_TOKEN не задан в .env")
    bot = telegram.get_bot()
    # Состояния диалогов (оформление, переписка) переживают перезапуск бота
    dp = build_dispatcher(RedisStorage.from_url(settings.REDIS_URL))
    await bot.set_my_commands([
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="catalog", description="Каталог"),
        BotCommand(command="search", description="Поиск по модели и детали"),
        BotCommand(command="cart", description="Корзина"),
        BotCommand(command="orders", description="Мои заказы"),
        BotCommand(command="profile", description="Профиль и оптовые цены"),
    ])
    url = webapp_url()
    if url:
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Магазин", web_app=WebAppInfo(url=url)))
    else:
        logger.warning("SITE_URL не https — кнопка Mini App выключена (Telegram открывает только https)")
    me = await bot.get_me()
    logger.info("Бот @%s запущен, сайт %s", me.username, settings.SITE_URL)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None)
