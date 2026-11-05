"""
Бот для администратора: быстрые действия из уведомлений (статус заказа, заявка на опт, ответ покупателю)
и сводка. Полная админ-панель — на сайте (/admin): бот и сайт работают с одними данными.
Права проверяются в каждом обработчике: callback-данные присылает клиент.
"""
import html

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.bot.common import BTN_ADMIN, esc, kb, main_menu, site
from app.models import Order, User, WholesaleApplication
from app.models.chat import ChatStatus, ChatThread
from app.models.order import OrderStatus
from app.models.user import UserRole
from app.models.wholesale_application import ApplicationStatus
from app.services import chat, order_service, telegram

router = Router(name="staff")


class StaffReply(StatesGroup):
    text = State()


async def _deny(event) -> bool:
    if isinstance(event, CallbackQuery):
        await event.answer("Недостаточно прав", show_alert=True)
    else:
        await event.answer("Недостаточно прав")
    return True


@router.message(F.text == BTN_ADMIN)
@router.message(Command("admin"))
async def admin_summary(message: Message, db: AsyncSession, user: User):
    if not user.is_superuser:
        await _deny(message)
        return
    new_orders = (await db.execute(select(func.count(Order.id)).where(Order.status == OrderStatus.PENDING))).scalar_one()
    chats = await chat.unread_for_staff(db)
    wholesale = (await db.execute(select(func.count(WholesaleApplication.id))
                                  .where(WholesaleApplication.status == ApplicationStatus.PENDING))).scalar_one()
    await message.answer(
        f"⚙️ <b>Админка</b>\n\nНовые заказы: <b>{new_orders}</b>\nОбращения без ответа: <b>{chats}</b>\n"
        f"Заявки на опт: <b>{wholesale}</b>\n\nУведомления о заказах, обращениях и заявках приходят сюда — "
        "с кнопками для быстрых действий. Остальное — в админ-панели сайта.",
        reply_markup=kb([
            [("Заказы", "url:" + site("/admin/orders?status=PENDING")), ("Обращения", "url:" + site("/admin/chats"))],
            [("Открыть админ-панель", "url:" + site("/admin/dashboard"))],
        ]),
    )


@router.callback_query(F.data.regexp(r"^ord:\d+:[A-Z]+$"))
async def order_status(cb: CallbackQuery, db: AsyncSession, user: User):
    if not user.is_superuser:
        await _deny(cb)
        return
    _, order_id, status = cb.data.split(":")
    if status not in OrderStatus.__members__:
        await cb.answer("Неизвестный статус", show_alert=True)
        return
    current = await db.get(Order, int(order_id))
    if current and current.status.name == status:
        await cb.answer("Статус уже установлен")
        return
    try:
        order = await order_service.change_order_status(db, int(order_id), OrderStatus[status], user.id)
        await db.commit()
    except order_service.OrderError as e:
        await cb.answer(str(e), show_alert=True)
        return
    label = telegram.ORDER_STATUS_TEXT[status]
    await cb.answer(f"Заказ № {order_id}: {label}")
    try:
        await cb.message.edit_text(cb.message.html_text + f"\n\n➡️ <b>{label}</b> — {esc(user.full_name or user.username)}",
                                   reply_markup=telegram.keyboard(telegram.order_admin_rows(order)))
    except Exception:
        pass
    await telegram.notify_order_status(int(order_id))


@router.callback_query(F.data.regexp(r"^ws:\d+:(APPROVED|REJECTED)$"))
async def wholesale_decision(cb: CallbackQuery, db: AsyncSession, user: User):
    if not user.is_superuser:
        await _deny(cb)
        return
    _, app_id, status = cb.data.split(":")
    application = (await db.execute(select(WholesaleApplication).options(joinedload(WholesaleApplication.user))
                                    .where(WholesaleApplication.id == int(app_id)))).scalar_one_or_none()
    if not application or application.status != ApplicationStatus.PENDING:
        await cb.answer("Заявка уже обработана", show_alert=True)
        return
    application.status = ApplicationStatus[status]
    approved = status == "APPROVED"
    if approved and application.user.role == UserRole.RETAIL:
        application.user.role = UserRole.WHOLESALE
    await db.commit()
    await cb.answer("Одобрено" if approved else "Отклонено")
    try:
        await cb.message.edit_text(cb.message.html_text + f"\n\n➡️ <b>{'Одобрено' if approved else 'Отклонено'}</b>", reply_markup=None)
    except Exception:
        pass
    await telegram.notify_wholesale_result(application.user_id, approved)


@router.callback_query(F.data.regexp(r"^chat:\d+:reply$"))
async def reply_start(cb: CallbackQuery, db: AsyncSession, user: User, state: FSMContext):
    if not user.is_superuser:
        await _deny(cb)
        return
    thread_id = int(cb.data.split(":")[1])
    await state.set_state(StaffReply.text)
    await state.update_data(thread_id=thread_id)
    await cb.message.answer(f"✍️ Ответ покупателю (обращение № {thread_id}). Напишите текст — он уйдёт в чат на сайте "
                            "и в Telegram покупателя. /cancel — отмена.")
    await cb.answer()


@router.message(StaffReply.text, Command("cancel"))
async def reply_cancel(message: Message, user: User, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu(user))


@router.message(StaffReply.text, F.text)
async def reply_send(message: Message, db: AsyncSession, user: User, state: FSMContext):
    if not user.is_superuser:
        await state.clear()
        return
    thread_id = (await state.get_data()).get("thread_id")
    await state.clear()
    thread = await db.get(ChatThread, thread_id)
    if not thread:
        await message.answer("Обращение не найдено.")
        return
    try:
        msg = await chat.add_message(db, thread, "staff", message.text, author_id=user.id)
    except chat.ChatError as e:
        await message.answer(html.escape(str(e)))
        return
    thread.unread_staff = 0
    if thread.assigned_to is None:
        thread.assigned_to = user.id
    if thread.status == ChatStatus.CLOSED:
        thread.status = ChatStatus.OPEN
    await db.commit()
    await message.answer("✓ Отправлено.", reply_markup=main_menu(user))
    await telegram.notify_client_reply(thread.id, msg.text)
