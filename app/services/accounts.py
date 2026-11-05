"""
Аккаунты и Telegram: покупатель из бота получает аккаунт сайта автоматически, а привязка Telegram
к существующему аккаунту переносит в него всё, что было сделано в боте (заказы, корзину, переписку).
"""
import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import parse_qsl

from sqlalchemy import select, update, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import InventoryTransaction, Order, User, UserCart, WholesaleApplication
from app.models.chat import ChatMessage, ChatThread
from app.models.user import UserRole

# Пароль у аккаунта из бота не задан: вход через Telegram, пароль можно установить в личном кабинете
NO_PASSWORD = "!telegram"


class AccountError(Exception):
    pass


def session_fingerprint(user: User) -> str:
    """Отпечаток пароля для сессии: сменили пароль — старые сессии (cookie) перестают действовать."""
    return hashlib.sha256((user.password_hash or "").encode()).hexdigest()[:16]


def has_password(user: User) -> bool:
    return bool(user.password_hash) and user.password_hash != NO_PASSWORD


def new_password_reset(user: User) -> str:
    """Одноразовый токен на 30 минут, связанный с текущим паролем."""
    from app.services import telegram
    return telegram.new_token("reset", json.dumps({"id": user.id, "pwv": session_fingerprint(user)}))


async def consume_password_reset(db: AsyncSession, token: str) -> User | None:
    from app.services import telegram
    raw = telegram.peek_token("reset", token)
    try:
        data = json.loads(raw or "null")
        user_id = int(data["id"])
        pwv = data["pwv"]
    except (ValueError, TypeError, KeyError):
        return None
    user = (await db.execute(select(User).where(User.id == user_id).with_for_update())).scalar_one_or_none()
    if not user or not user.can_sign_in or not hmac.compare_digest(session_fingerprint(user), pwv):
        return None
    return user if telegram.take_token("reset", token) == raw else None


def is_auto_account(user: User) -> bool:
    """Аккаунт, созданный ботом, которым ещё не пользовались на сайте (без пароля)."""
    return not has_password(user) and user.username.startswith("tg")


async def telegram_user(db: AsyncSession, tg_id: int, full_name: str = "", tg_username: str = "") -> User:
    """Аккаунт по Telegram id; если его нет — создаётся (покупатель, розница)."""
    user = (await db.execute(select(User).where(User.telegram_id == tg_id))).scalar_one_or_none()
    if user:
        if full_name and not user.full_name:
            user.full_name = full_name[:200]
            await db.commit()
        return user
    username = f"tg{tg_id}"
    if (await db.execute(select(User.id).where(User.username == username))).first():
        username = f"tg{tg_id}_{secrets.token_hex(2)}"  # имя занято (до запрета «tg<цифры>» при регистрации)
    user = User(username=username, password_hash=NO_PASSWORD, telegram_id=tg_id,
                full_name=(full_name or tg_username or None) and (full_name or tg_username)[:200])
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def link_telegram(db: AsyncSession, user: User, tg_id: int, full_name: str = "") -> None:
    """Привязать Telegram к аккаунту сайта. Если бот уже создал для этого Telegram отдельный аккаунт —
    заказы, корзина, переписка и заявки переезжают в аккаунт сайта, а автоаккаунт удаляется."""
    other = (await db.execute(select(User).where(User.telegram_id == tg_id))).scalar_one_or_none()
    if other and other.id != user.id:
        if not is_auto_account(other):
            raise AccountError("Этот Telegram уже привязан к другому аккаунту сайта")
        await _merge_into(db, other, user)
    user.telegram_id = tg_id
    if full_name and not user.full_name:
        user.full_name = full_name[:200]
    await db.commit()


async def _merge_into(db: AsyncSession, src: User, dst: User) -> None:
    await db.execute(update(Order).where(Order.user_id == src.id).values(user_id=dst.id))
    await db.execute(update(WholesaleApplication).where(WholesaleApplication.user_id == src.id).values(user_id=dst.id))
    await db.execute(update(InventoryTransaction).where(InventoryTransaction.user_id == src.id).values(user_id=dst.id))
    await db.execute(update(ChatMessage).where(ChatMessage.author_id == src.id).values(author_id=dst.id))
    # Корзина: позиции, которых нет у аккаунта сайта, переносим; совпадающие оставляем как есть
    have = set((await db.execute(select(UserCart.product_id).where(UserCart.user_id == dst.id))).scalars())
    for row in (await db.execute(select(UserCart).where(UserCart.user_id == src.id))).scalars().all():
        if row.product_id in have:
            await db.delete(row)
        else:
            row.user_id = dst.id
    # Переписка: в один диалог
    own = (await db.execute(select(ChatThread).where(ChatThread.user_id == dst.id)
                            .order_by(ChatThread.last_message_at.desc()).limit(1))).scalar_one_or_none()
    for thread in (await db.execute(select(ChatThread).where(ChatThread.user_id == src.id))).scalars().all():
        if own is None:
            thread.user_id = dst.id
            own = thread
        else:
            await db.execute(update(ChatMessage).where(ChatMessage.thread_id == thread.id).values(thread_id=own.id))
            own.unread_staff += thread.unread_staff
            own.unread_client += thread.unread_client
            await db.execute(delete(ChatThread).where(ChatThread.id == thread.id))
    await db.execute(update(ChatThread).where(ChatThread.assigned_to == src.id).values(assigned_to=dst.id))
    # Права не теряются: администратор (ADMIN_ID создаёт его автоаккаунт) остаётся администратором, оптовик — оптовиком
    if src.is_superuser:
        dst.role, dst.is_admin = UserRole.ADMIN, True
    elif src.role == UserRole.WHOLESALE and dst.role == UserRole.RETAIL:
        dst.role = UserRole.WHOLESALE
    dst.phone = dst.phone or src.phone
    src.telegram_id = None
    await db.flush()
    await db.delete(src)
    await db.flush()


def check_webapp_init_data(init_data: str, max_age: int = 3600) -> dict | None:
    """Подпись Telegram.WebApp.initData (Mini App). Возвращает объект user или None."""
    if not init_data or not settings.TELEGRAM_BOT_TOKEN:
        return None
    try:
        data = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None
    received = data.pop("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", settings.TELEGRAM_BOT_TOKEN.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(hmac.new(secret, check.encode(), hashlib.sha256).hexdigest(), received):
        return None
    try:
        if time.time() - int(data.get("auth_date", "0")) > max_age:
            return None
        user = json.loads(data.get("user", "null"))
    except ValueError:
        return None
    return user if isinstance(user, dict) and isinstance(user.get("id"), int) else None
