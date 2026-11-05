"""
Telegram: уведомления с сайта и общие функции для бота (python -m app.bot).

Сайт и бот работают на одной базе, поэтому бот — это ещё один интерфейс магазина:
те же товары, цены, остатки, корзина, заказы и обращения.

Отправка не должна ломать запрос сайта: notify_* запускаются фоном (fire), ошибки только логируются.
Без TELEGRAM_BOT_TOKEN всё молча выключено.
"""
import asyncio
import html
import logging
import secrets
from decimal import Decimal

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import AsyncSessionLocal

logger = logging.getLogger(__name__)

_bot: Bot | None = None
_tasks: set[asyncio.Task] = set()

ORDER_STATUS_TEXT = {"PENDING": "Новый", "PAID": "Оплачен", "COMPLETED": "Выполнен", "CANCELLED": "Отменён"}


def enabled() -> bool:
    return bool(settings.TELEGRAM_BOT_TOKEN)


def get_bot() -> Bot | None:
    """Один экземпляр Bot на процесс (сайт и бот создают свой)."""
    global _bot
    if _bot is None and enabled():
        session = None
        if settings.TELEGRAM_API_URL:
            session = AiohttpSession(api=TelegramAPIServer.from_base(settings.TELEGRAM_API_URL))
        _bot = Bot(settings.TELEGRAM_BOT_TOKEN, session=session,
                   default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
    return _bot


def site(path: str = "") -> str:
    return settings.SITE_URL.rstrip("/") + path


def bot_link(start: str = "") -> str:
    name = settings.TELEGRAM_BOT_USERNAME.lstrip("@")
    return f"https://t.me/{name}" + (f"?start={start}" if start else "") if name else ""


def money(value) -> str:
    """Как макрос money на сайте: «2 875,50 ₽», «208 ₽»."""
    v = Decimal(value or 0).quantize(Decimal("0.01"))
    if v != v.to_integral_value():
        s = "{:,.2f}".format(v).replace(",", " ").replace(".", ",")
    else:
        s = "{:,.0f}".format(v).replace(",", " ")
    return s + " ₽"


def keyboard(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup | None:
    """[[(текст, "url:…" | callback_data)]] → InlineKeyboardMarkup."""
    rows = [[(t, v) for t, v in row if not v.startswith("url:") or v.startswith("url:https://")] for row in rows or []]
    rows = [row for row in rows if row]
    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, url=v[4:]) if v.startswith("url:") else InlineKeyboardButton(text=t, callback_data=v)
         for t, v in row]
        for row in rows
    ])


async def send(chat_id: int, text: str, rows: list[list[tuple[str, str]]] | None = None) -> bool:
    bot = get_bot()
    if not bot or not chat_id:
        return False
    try:
        await bot.send_message(chat_id, text, reply_markup=keyboard(rows or []))
        return True
    except Exception:  # заблокировал бота, удалил аккаунт, сеть — не критично
        logger.warning("Telegram: не удалось отправить сообщение %s", chat_id, exc_info=True)
        return False


def fire(coro) -> None:
    """Запустить отправку фоном, не задерживая ответ сайта."""
    if not enabled():
        coro.close()
        return
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    task.add_done_callback(lambda t: t.exception() and logger.error("Telegram: %s", t.exception()))


async def admin_chat_ids(db: AsyncSession) -> list[int]:
    from app.models import User
    from app.models.user import UserRole
    rows = await db.execute(
        select(User.telegram_id).where(User.telegram_id.isnot(None), or_(User.role == UserRole.ADMIN, User.is_admin.is_(True)))
    )
    return [r[0] for r in rows]


async def notify_admins(text: str, rows: list[list[tuple[str, str]]] | None = None) -> None:
    async with AsyncSessionLocal() as db:
        ids = await admin_chat_ids(db)
    await asyncio.gather(*(send(i, text, rows) for i in ids))


# ───────────────────────── Заказы ─────────────────────────

async def _load_order(db: AsyncSession, order_id: int):
    from app.models import Order, OrderItem
    return (
        await db.execute(
            select(Order)
            .options(selectinload(Order.items).selectinload(OrderItem.product), selectinload(Order.user),
                     selectinload(Order.city))
            .where(Order.id == order_id)
        )
    ).scalar_one_or_none()


def order_lines(order, limit: int = 15) -> str:
    lines = [f"• {html.escape(it.product.name if it.product else '—')} — {it.quantity} × {money(it.price_at_time)}"
             for it in order.items[:limit]]
    if len(order.items) > limit:
        lines.append(f"…и ещё {len(order.items) - limit} поз.")
    return "\n".join(lines)


def order_admin_rows(order) -> list[list[tuple[str, str]]]:
    rows = []
    st = order.status.name
    actions = {"PENDING": [("✅ Оплачен", "PAID"), ("❌ Отменить", "CANCELLED")],
               "PAID": [("📦 Выполнен", "COMPLETED"), ("❌ Отменить", "CANCELLED")]}.get(st, [])
    if actions:
        rows.append([(t, f"ord:{order.id}:{s}") for t, s in actions])
    rows.append([("Открыть в админке", "url:" + site(f"/admin/orders/{order.id}"))])
    return rows


async def notify_new_order(order_id: int) -> None:
    async with AsyncSessionLocal() as db:
        order = await _load_order(db, order_id)
        if not order:
            return
        src = {"bot": " · из Telegram", "admin": " · оформил менеджер"}.get(order.source, "")
        opt = " · ОПТ" if order.order_type.name == "WHOLESALE" else ""
        text = (
            f"🛒 <b>Новый заказ № {order.id}</b>{src}{opt}\n"
            f"{html.escape(order.customer_name)}, {html.escape(order.customer_phone)}\n"
            f"Склад: {html.escape(order.city.name if order.city else '—')}\n\n"
            f"{order_lines(order)}\n\n<b>Итого: {money(order.total_amount)}</b>"
            + (f"\n💬 {html.escape(order.comment)}" if order.comment else "")
        )
        ids = await admin_chat_ids(db)
        await asyncio.gather(*(send(i, text, order_admin_rows(order)) for i in ids))
        if order.user and order.user.telegram_id:
            await send(order.user.telegram_id,
                       f"✅ <b>Заказ № {order.id} принят</b>\n\n{order_lines(order)}\n\n<b>Итого: {money(order.total_amount)}</b>\n"
                       f"Склад: {html.escape(order.city.name if order.city else '—')}\n\n"
                       "Менеджер свяжется с вами для подтверждения. Статус пришлём сюда.",
                       [[("Мои заказы", "url:" + site("/auth/account"))]])


async def notify_order_status(order_id: int) -> None:
    async with AsyncSessionLocal() as db:
        order = await _load_order(db, order_id)
        if not order or not order.user or not order.user.telegram_id:
            return
        text = {
            "PAID": f"💳 Заказ № {order.id} оплачен. Готовим к выдаче.",
            "COMPLETED": f"📦 Заказ № {order.id} выполнен. Спасибо за покупку!",
            "CANCELLED": f"❌ Заказ № {order.id} отменён. Если это ошибка — напишите нам.",
            "PENDING": f"↩️ Заказ № {order.id} снова в работе.",
        }[order.status.name]
        await send(order.user.telegram_id, text)


# ───────────────────────── Обращения и опт ─────────────────────────

async def notify_chat_message(thread_id: int, text: str) -> None:
    """Новое сообщение покупателя — администраторам (с кнопкой «Ответить» прямо в Telegram)."""
    from app.models.chat import ChatThread
    async with AsyncSessionLocal() as db:
        thread = (await db.execute(select(ChatThread).options(selectinload(ChatThread.user))
                                   .where(ChatThread.id == thread_id))).scalar_one_or_none()
        if not thread:
            return
        who = (thread.user.full_name or thread.user.username) if thread.user else (thread.name or "Гость")
        phone = thread.phone or (thread.user.phone if thread.user else None)
        body = (f"💬 <b>Обращение № {thread.id}</b> — {html.escape(who)}" + (f", {html.escape(phone)}" if phone else "")
                + f"\n\n{html.escape(text[:1500])}")
        ids = await admin_chat_ids(db)
    rows = [[("✍️ Ответить", f"chat:{thread_id}:reply"), ("Открыть", "url:" + site(f"/admin/chats/{thread_id}"))]]
    await asyncio.gather(*(send(i, body, rows) for i in ids))


async def notify_client_reply(thread_id: int, text: str) -> None:
    """Ответ менеджера — покупателю в Telegram, если он привязан (переписка та же, что на сайте)."""
    from app.models.chat import ChatThread
    async with AsyncSessionLocal() as db:
        thread = (await db.execute(select(ChatThread).options(selectinload(ChatThread.user))
                                   .where(ChatThread.id == thread_id))).scalar_one_or_none()
        if not thread or not thread.user or not thread.user.telegram_id:
            return
        tg = thread.user.telegram_id
    await send(tg, f"💬 <b>Менеджер Parts.Site:</b>\n{html.escape(text)}", [[("Ответить", "chat:me:reply")]])


async def notify_wholesale(application_id: int) -> None:
    from app.models import WholesaleApplication
    async with AsyncSessionLocal() as db:
        app_ = (await db.execute(select(WholesaleApplication).options(selectinload(WholesaleApplication.user))
                                 .where(WholesaleApplication.id == application_id))).scalar_one_or_none()
        if not app_:
            return
        who = app_.user.full_name or app_.user.username if app_.user else "—"
        text = f"💼 <b>Заявка на опт № {app_.id}</b> — {html.escape(who)}\n\n{html.escape(app_.company_details or '')}"
    await notify_admins(text, [[("✅ Одобрить", f"ws:{application_id}:APPROVED"), ("❌ Отклонить", f"ws:{application_id}:REJECTED")],
                               [("Открыть в админке", "url:" + site("/admin/wholesale"))]])


async def notify_wholesale_result(user_id: int, approved: bool) -> None:
    from app.models import User
    async with AsyncSessionLocal() as db:
        tg = (await db.execute(select(User.telegram_id).where(User.id == user_id))).scalar_one_or_none()
    if tg:
        await send(tg, "🎉 Заявка на оптовые цены одобрена — в каталоге теперь оптовые цены." if approved
                   else "Заявка на оптовые цены отклонена. Подробности — у менеджера.")


# ───────────────────────── Одноразовые токены (Redis) ─────────────────────────
# link:<t> → id аккаунта (привязать Telegram), login:<t> → «» / id аккаунта (вход через бота),
# reset:<t> → id аккаунта (сброс пароля по ссылке из бота)

TTL = {"link": 600, "login": 600, "reset": 1800}


def _redis():
    from app.services.cart import redis_client
    return redis_client


def new_token(kind: str, value: str | int = "") -> str:
    token = secrets.token_urlsafe(18).replace("-", "x").replace("_", "y")  # deep-link: только [A-Za-z0-9]
    _redis().set(f"tg:{kind}:{token}", str(value), ex=TTL[kind])
    return token


def peek_token(kind: str, token: str) -> str | None:
    return _redis().get(f"tg:{kind}:{token}") if token else None


def set_token(kind: str, token: str, value: str | int) -> bool:
    """Записать результат в существующий токен (бот подтвердил вход)."""
    return bool(_redis().set(f"tg:{kind}:{token}", str(value), ex=TTL[kind], xx=True))


def take_token(kind: str, token: str) -> str | None:
    if not token:
        return None
    return _redis().getdel(f"tg:{kind}:{token}")
