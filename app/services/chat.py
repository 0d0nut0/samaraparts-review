"""
Чат покупателя с менеджером.

Покупатель определяется по аккаунту (user_id) или, для гостя, по токену в сессии (chat_token).
Когда гость входит в аккаунт, его диалог привязывается к аккаунту.
Клиент и админка опрашивают сервер каждые несколько секунд (?after=<id последнего сообщения>).
"""
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime

from sqlalchemy import select, func, update, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import ChatMessage, ChatStatus, ChatThread

MAX_TEXT = 2000
RATE_WINDOW, RATE_LIMIT = 30, 10  # не больше 10 сообщений за 30 секунд с одного диалога
IP_WINDOW, IP_LIMIT = 600, 60      # и не больше 60 за 10 минут с одного IP (новая cookie — новый диалог)
_recent: dict[object, deque] = defaultdict(deque)

QUICK_REPLIES = [
    "Здравствуйте! Сейчас посмотрю наличие.",
    "Товар есть в наличии, могу оформить заказ на вас.",
    "Уточните, пожалуйста, модель устройства (например, A2221 на корпусе).",
    "Заказ оформлен, номер отправлю следующим сообщением.",
    "Спасибо за обращение! Если появятся вопросы — пишите.",
]


class ChatError(Exception):
    pass


def rate_limited(key, window: int = RATE_WINDOW, limit: int = RATE_LIMIT) -> bool:
    now = time.monotonic()
    q = _recent[key]
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        return True
    q.append(now)
    if len(_recent) > 10_000:  # чистим ключи без свежих сообщений, чтобы словарь не рос бесконечно
        for k in [k for k, v in _recent.items() if not v or now - v[-1] > IP_WINDOW]:
            _recent.pop(k, None)
    return False


async def find_thread(request, db: AsyncSession) -> ChatThread | None:
    """Диалог текущего посетителя (без создания). Гостевой диалог при входе привязывается к аккаунту."""
    user_id = request.session.get("user_id")
    token = request.session.get("chat_token")
    guest = None
    if token:
        guest = (await db.execute(select(ChatThread).where(ChatThread.guest_token == token))).scalar_one_or_none()
    if not user_id:
        return guest
    own = (
        await db.execute(
            select(ChatThread).where(ChatThread.user_id == user_id).order_by(ChatThread.last_message_at.desc()).limit(1)
        )
    ).scalar_one_or_none()
    if guest and guest.user_id is None:
        if own is None:
            guest.user_id = user_id
            guest.guest_token = None
            own = guest
        else:
            # переносим гостевую переписку в диалог аккаунта (без ленивой загрузки — async-сессия)
            await db.execute(update(ChatMessage).where(ChatMessage.thread_id == guest.id).values(thread_id=own.id))
            own.unread_staff += guest.unread_staff
            await db.execute(delete(ChatThread).where(ChatThread.id == guest.id))
        request.session.pop("chat_token", None)
        await db.commit()
    return own


async def get_or_create_thread(request, db: AsyncSession) -> ChatThread:
    thread = await find_thread(request, db)
    if thread:
        return thread
    thread = ChatThread(user_id=request.session.get("user_id"))
    if not thread.user_id:
        token = secrets.token_urlsafe(24)
        request.session["chat_token"] = token
        thread.guest_token = token
    db.add(thread)
    await db.flush()
    return thread


async def add_message(db: AsyncSession, thread: ChatThread, sender: str, text: str, author_id: int | None = None) -> ChatMessage:
    text = (text or "").strip()
    if not text:
        raise ChatError("Пустое сообщение")
    if len(text) > MAX_TEXT:
        raise ChatError(f"Сообщение длиннее {MAX_TEXT} символов")
    msg = ChatMessage(thread_id=thread.id, sender=sender, text=text, author_id=author_id)
    db.add(msg)
    thread.last_message_at = datetime.now()
    if sender == "client":
        thread.unread_staff += 1
        if thread.status == ChatStatus.CLOSED:  # новое сообщение переоткрывает обращение
            thread.status = ChatStatus.OPEN
    elif sender == "staff":
        thread.unread_client += 1
    await db.flush()
    return msg


async def messages_after(db: AsyncSession, thread_id: int, after: int = 0, limit: int = 200) -> list[ChatMessage]:
    return (
        await db.execute(
            select(ChatMessage).where(ChatMessage.thread_id == thread_id, ChatMessage.id > after)
            .order_by(ChatMessage.id).limit(limit)
        )
    ).scalars().all()


def serialize(m: ChatMessage, staff_names: dict[int, str] | None = None) -> dict:
    return {
        "id": m.id,
        "sender": m.sender,
        "text": m.text,
        "time": m.created_at.strftime("%H:%M") if m.created_at else "",
        "date": m.created_at.strftime("%d.%m.%Y") if m.created_at else "",
        "author": (staff_names or {}).get(m.author_id) if m.sender == "staff" else None,
    }


async def unread_for_staff(db: AsyncSession) -> int:
    """Сколько открытых обращений ждут ответа (для счётчика в меню админки)."""
    return (
        await db.execute(
            select(func.count(ChatThread.id)).where(ChatThread.status == ChatStatus.OPEN, ChatThread.unread_staff > 0)
        )
    ).scalar_one()
