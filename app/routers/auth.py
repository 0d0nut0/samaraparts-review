"""
Модуль авторизации: регистрация, вход, выход, личный кабинет.
Хеширование паролей через стандартный hashlib (PBKDF2-SHA256),
без сторонних зависимостей — совместимо с любой версией Python 3.
"""

import hashlib
import hmac
import os
import asyncio
import html
import json
import re

from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select, insert, delete, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.config import settings
from app.models import Order, OrderItem, Product, UserCart, WholesaleApplication
from app.models.user import User
from app.models.wholesale_application import ApplicationStatus
from app.services import accounts, cart, telegram, email_verification
from app.services.passwords import password_error
from app.templating import templates, ROLE_LABELS

router = APIRouter(prefix="/auth", tags=["auth"])

LOGIN_ATTEMPTS = 10


# ---------------------------------------------------------------------------
# Утилиты для работы с паролями
# ---------------------------------------------------------------------------

def _hash_password(password: str, salt: str | None = None) -> str:
    """
    Хешируем пароль через PBKDF2-SHA256.
    Формат итоговой строки: pbkdf2_sha256$<соль>$<хеш>.
    """
    if salt is None:
        salt = os.urandom(16).hex()
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260_000)
    return f"pbkdf2_sha256${salt}${key.hex()}"


def verify_password(plain: str, stored: str) -> bool:
    """
    Проверяем пароль, пересчитывая хеш с той же солью.
    Возвращает True, если пароли совпадают.
    """
    try:
        _, salt, _ = stored.split("$")
    except ValueError:
        return False
    return hmac.compare_digest(_hash_password(plain, salt), stored)


def get_password_hash(password: str) -> str:
    """Создаёт хеш пароля с рандомной солью."""
    return _hash_password(password)


# ---------------------------------------------------------------------------
# Вспомогательная функция для получения текущего пользователя из сессии
# ---------------------------------------------------------------------------

async def get_current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User | None:
    """
    Читает user_id из cookie-сессии и возвращает объект пользователя.
    Если сессии нет или пользователь не найден — вернёт None.
    """
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user and user.can_sign_in and request.session.get("pwv") == accounts.session_fingerprint(user):
        return user
    return None


async def _merge_guest_cart(db: AsyncSession, user_id: int, cart_id: str, guest_items: list[dict]) -> None:
    """Гостевая корзина (Redis) → корзина аккаунта; количество из гостевой заменяет сохранённое для тех же товаров."""
    if not guest_items:
        return
    skus = [item["sku"] for item in guest_items]
    prod_map = {p.sku: p.id for p in (await db.execute(select(Product).where(Product.sku.in_(skus)))).scalars()}
    rows = [
        {"user_id": user_id, "product_id": prod_map[item["sku"]], "quantity": item["qty"]}
        for item in guest_items if item["sku"] in prod_map
    ]
    if rows:
        await db.execute(delete(UserCart).where(
            UserCart.user_id == user_id, UserCart.product_id.in_([r["product_id"] for r in rows])))
        await db.execute(insert(UserCart).values(rows))
        await db.commit()
    cart.redis_client.delete(cart._redis_key(cart_id))


def _reset_session(request: Request) -> None:
    """Новая сессия при входе (защита от session fixation), но гостевой диалог чата сохраняем —
    services/chat.find_thread привяжет его к аккаунту."""
    chat_token = request.session.get("chat_token")
    city_id = request.session.get("city_id")
    request.session.clear()
    if chat_token:
        request.session["chat_token"] = chat_token
    if city_id:
        request.session["city_id"] = city_id


async def _login_user(request: Request, db: AsyncSession, user: User) -> None:
    """Вход: новая сессия (защита от session fixation), гостевая корзина переезжает в аккаунт."""
    if not user.can_sign_in:
        raise HTTPException(status_code=403, detail="Подтвердите e-mail перед входом в аккаунт")
    cart_id = cart.get_cart_id(request)
    guest_items = await cart.get_cart_items(request, db)
    _reset_session(request)
    request.session["user_id"] = user.id
    request.session["pwv"] = accounts.session_fingerprint(user)
    await _merge_guest_cart(db, user.id, cart_id, guest_items)


def _safe_next(value: str | None, default: str) -> str:
    """Куда вернуть после входа: только относительный путь этого сайта (защита от open redirect)."""
    if value and value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    return default


# ---------------------------------------------------------------------------
# Эндпоинты авторизации
# ---------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    """Форма входа."""
    return templates.TemplateResponse(request=request, name="login.html", context={"next": request.query_params.get("next", "")})


@router.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    """Обрабатывает POST-форму входа. При успехе — редирект на главную."""
    username = username.strip()
    # Не больше 10 неудачных попыток за 15 минут на логин и на IP (задержка в 1 с не спасает от параллельного перебора)
    ip = request.client.host if request.client else "?"
    keys = [f"login_fail:u:{username.lower()}", f"login_fail:ip:{ip}"]
    if any(int(cart.redis_client.get(k) or 0) >= LOGIN_ATTEMPTS for k in keys):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Слишком много попыток входа. Подождите 15 минут или войдите через Telegram.",
                     "next": next, "username": username},
            status_code=429,
        )
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()

    if not user or len(password) > 128 or not verify_password(password, user.password_hash):
        for k in keys:
            cart.redis_client.incr(k)
            cart.redis_client.expire(k, 900)
        await asyncio.sleep(1) # Защита от брутфорса
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Неверное имя пользователя или пароль", "next": next, "username": username},
        )

    if not user.can_sign_in:
        return templates.TemplateResponse(request=request, name="verify_email.html", context={
            "pending": True, "email": user.email, "next": next,
            "error": "Подтвердите e-mail по ссылке из письма, прежде чем войти."}, status_code=403)
    for key in keys:
        cart.redis_client.delete(key)
    await _login_user(request, db, user)
    return RedirectResponse(url=_safe_next(next, "/catalog"), status_code=303)


@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    """Форма регистрации."""
    return templates.TemplateResponse(request=request, name="register.html", context={
        "next": request.query_params.get("next", ""), "email_required": settings.REGISTRATION_EMAIL_ENABLED})


@router.post("/register")
async def register(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(""),
    email: str = Form(""),
    next: str = Form(""),
    phone: str = Form(""),
    consent: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    """При включённой почте вход требует подтверждения адреса; иначе адрес не собирается."""
    username, phone = username.strip(), phone.strip()[:50]
    email_required = settings.REGISTRATION_EMAIL_ENABLED
    normalized = email_verification.normalize_email(email) if email_required else None
    context = {"next": next, "username": username, "phone": phone, "email": email,
               "email_required": email_required}

    def error(message, status=400):
        return templates.TemplateResponse(request=request, name="register.html",
                                          context={**context, "error": message}, status_code=status)

    if not consent:
        return error("Нужно согласие на обработку персональных данных")
    if not (3 <= len(username) <= 50):
        return error("Имя пользователя должно содержать от 3 до 50 символов")
    if re.fullmatch(r"tg\d+(_.*)?", username, re.IGNORECASE):
        return error("Имена вида «tg123» заняты под аккаунты из Telegram — выберите другое")
    if email_required and not normalized:
        return error("Укажите корректный адрес e-mail")
    if message := password_error(password, password_confirm):
        return error(message)
    ip = request.client.host if request.client else "?"
    limit_key = f"register:ip:{ip}"
    if cart.redis_client.incr(limit_key) == 1:
        cart.redis_client.expire(limit_key, 900)
    if int(cart.redis_client.get(limit_key) or 0) > 10:
        return error("Слишком много попыток регистрации. Подождите 15 минут.", 429)
    if email_required and not email_verification.configured():
        return error("Почта временно недоступна. Регистрация будет доступна немного позже.", 503)
    identity_checks = [func.lower(User.username) == username.lower()]
    if normalized:
        identity_checks.append(User.email == normalized)
    duplicate = (await db.execute(select(User.id).where(or_(*identity_checks)))).first()
    if duplicate:
        return error("Имя пользователя или e-mail уже используются. Войдите или запросите письмо подтверждения повторно."
                     if email_required else "Это имя пользователя уже занято. Выберите другое или войдите.")
    user = User(username=username, password_hash=get_password_hash(password), phone=phone or None,
                email=normalized, email_verified=False)
    db.add(user)
    try:
        await db.commit()
        await db.refresh(user)
    except IntegrityError:
        await db.rollback()
        return error("Имя пользователя или e-mail уже используются.")
    if not email_required:
        # No unverified address is saved or represented as verified. Existing
        # pending email accounts still require their original verification.
        await _login_user(request, db, user)
        return RedirectResponse(url=_safe_next(next, "/catalog"), status_code=303)
    context = {"pending": True, "email": normalized, "next": next}
    try:
        await email_verification.send_verification(db, user, _safe_next(next, "/auth/account"))
        context["sent"] = True
    except email_verification.MailError as exc:
        context["error"] = str(exc)
    # Сессия и гостевая корзина сохраняются, но user_id до подтверждения не выдаём.
    return templates.TemplateResponse(request=request, name="verify_email.html", context=context)


@router.get("/verify-email", response_class=HTMLResponse)
async def verify_email_page(request: Request, token: str = "", next: str = "", db: AsyncSession = Depends(get_db)):
    # GET не расходует токен: почтовые сканеры могут открывать ссылки до покупателя.
    return templates.TemplateResponse(request=request, name="verify_email.html", context={
        "token": token, "valid": await email_verification.valid_token(db, token), "next": next})


@router.post("/verify-email")
async def verify_email(request: Request, token: str = Form(""), next: str = Form(""), db: AsyncSession = Depends(get_db)):
    if not await email_verification.confirm(db, token):
        return templates.TemplateResponse(request=request, name="verify_email.html", context={"valid": False}, status_code=400)
    from urllib.parse import urlencode
    return RedirectResponse("/auth/login?" + urlencode({"verified": "1", "next": _safe_next(next, "/auth/account")}), status_code=303)


@router.get("/resend-verification", response_class=HTMLResponse)
def resend_verification_page(request: Request, next: str = ""):
    return templates.TemplateResponse(request=request, name="verify_email.html", context={"pending": True, "next": next})


@router.post("/resend-verification", response_class=HTMLResponse)
async def resend_verification(request: Request, email: str = Form(""), next: str = Form(""), db: AsyncSession = Depends(get_db)):
    normalized = email_verification.normalize_email(email)
    ip = request.client.host if request.client else "?"
    digest = hashlib.sha256((normalized or "invalid").encode()).hexdigest()
    keys = [f"email_verify:ip:{ip}", f"email_verify:email:{digest}"]
    limited = False
    for key in keys:
        count = cart.redis_client.incr(key)
        if count == 1:
            cart.redis_client.expire(key, 900)
        limited = limited or count > 3
    if limited:
        return templates.TemplateResponse(request=request, name="verify_email.html", context={
            "pending": True, "email": email, "next": next,
            "error": "Слишком много запросов. Подождите 15 минут."}, status_code=429)
    if not email_verification.configured():
        return templates.TemplateResponse(request=request, name="verify_email.html", context={
            "pending": True, "email": email, "next": next,
            "error": "Почта временно недоступна. Попробуйте позже."}, status_code=503)
    user = (await db.execute(select(User).where(User.email == normalized, User.email_verified.is_(False)))).scalar_one_or_none() if normalized else None
    if user:
        try:
            await email_verification.send_verification(db, user, _safe_next(next, "/auth/account"))
        except email_verification.MailError:
            pass  # Одинаковый ответ не раскрывает наличие аккаунта по адресу.
    return templates.TemplateResponse(request=request, name="verify_email.html", context={
        "pending": True, "sent": True, "email": email, "next": next, "generic": True})


@router.get("/logout")
def logout(request: Request):
    """Выход: очищаем user_id из сессии, оставляем корзину нетронутой."""
    request.session.pop("user_id", None)
    return RedirectResponse(url="/", status_code=303)


@router.get("/account", response_class=HTMLResponse)
async def account_page(
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """
    Личный кабинет пользователя.
    Неавторизованного перебрасываем на страницу входа.
    """
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/auth/login", status_code=303)

    orders = (
        await db.execute(
            select(Order)
            .options(selectinload(Order.items).selectinload(OrderItem.product), selectinload(Order.city))
            .where(Order.user_id == user.id)
            .order_by(Order.created_at.desc())
            .limit(20)
        )
    ).scalars().all()
    application = (
        await db.execute(
            select(WholesaleApplication)
            .where(WholesaleApplication.user_id == user.id)
            .order_by(WholesaleApplication.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    return templates.TemplateResponse(
        request=request,
        name="account.html",
        context={
            "user": user,
            "orders": orders,
            "application": application,
            "role_label": ROLE_LABELS.get(user.role.name, user.role.name),
            "has_password": accounts.has_password(user),
            "bot_enabled": telegram.enabled() and bool(telegram.bot_link()),
            "msg": request.query_params.get("msg"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/wholesale")
async def request_wholesale(
    request: Request,
    company_details: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    """Заявка на оптовые цены из личного кабинета (модерируется в /admin/wholesale)."""
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/auth/login", status_code=303)

    pending = (
        await db.execute(
            select(WholesaleApplication.id).where(
                WholesaleApplication.user_id == user.id,
                WholesaleApplication.status == ApplicationStatus.PENDING,
            )
        )
    ).first()
    details = company_details.strip()
    if not user.is_wholesale and not pending and len(details) >= 3:
        application = WholesaleApplication(user_id=user.id, company_details=details[:2000])
        db.add(application)
        await db.commit()
        telegram.fire(telegram.notify_wholesale(application.id))
    return RedirectResponse(url="/auth/account", status_code=303)


@router.post("/orders/{order_id}/repeat")
async def repeat_order(order_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """«Повторить заказ»: кладёт позиции прошлого заказа в корзину (только свои заказы)."""
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/auth/login", status_code=303)
    order = (
        await db.execute(
            select(Order).options(selectinload(Order.items).selectinload(OrderItem.product))
            .where(Order.id == order_id, Order.user_id == user.id)
        )
    ).scalar_one_or_none()
    if order:
        for it in order.items:
            if it.product and it.product.is_active:
                await cart.add_to_cart(request, it.product.sku, it.quantity, db)
    return RedirectResponse(url="/auth/account?repeated=1", status_code=303)


# ---------------------------------------------------------------------------
# Пароль: смена в кабинете и восстановление через Telegram
# ---------------------------------------------------------------------------

def _account_back(msg: str | None = None, error: str | None = None) -> RedirectResponse:
    from urllib.parse import quote
    q = f"?msg={quote(msg)}" if msg else f"?error={quote(error)}" if error else ""
    return RedirectResponse(url="/auth/account" + q + "#security", status_code=303)


@router.post("/password")
async def change_password(request: Request, old_password: str = Form(""), new_password: str = Form(...),
                          password_confirm: str = Form(""), db: AsyncSession = Depends(get_db)):
    """Смена пароля. Аккаунт из Telegram-бота пароля не имеет — задаёт его без старого."""
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/auth/login", status_code=303)
    if accounts.has_password(user) and (len(old_password) > 128 or not verify_password(old_password, user.password_hash)):
        return _account_back(error="Текущий пароль указан неверно")
    if message := password_error(new_password, password_confirm):
        return _account_back(error=message)
    user.password_hash = get_password_hash(new_password)
    await db.commit()
    request.session["pwv"] = accounts.session_fingerprint(user)  # эта сессия остаётся, остальные выходят
    return _account_back(msg="Пароль сохранён. На других устройствах нужно войти заново.")


@router.get("/forgot", response_class=HTMLResponse)
def forgot_page(request: Request):
    return templates.TemplateResponse(request=request, name="forgot.html",
                                      context={"bot_enabled": telegram.enabled() and bool(telegram.bot_link())})


@router.post("/forgot", response_class=HTMLResponse)
async def forgot(request: Request, username: str = Form(...), db: AsyncSession = Depends(get_db)):
    """Ссылка на сброс пароля уходит в привязанный Telegram. Ответ одинаковый, есть аккаунт или нет."""
    username = username.strip()
    ip = request.client.host if request.client else "?"
    keys = [f"forgot:u:{username.lower()}", f"forgot:ip:{ip}"]
    limited = any(int(cart.redis_client.get(k) or 0) >= 3 for k in keys)
    for k in keys:
        cart.redis_client.incr(k)
        cart.redis_client.expire(k, 900)
    user = None if limited else (
        await db.execute(select(User).where(func.lower(User.username) == username.lower()))).scalar_one_or_none()
    if user and user.telegram_id:
        token = accounts.new_password_reset(user)
        url = telegram.site(f"/auth/reset?token={token}")
        await telegram.send(
            user.telegram_id,
            f"🔑 Восстановление пароля на Parts.Site для аккаунта <b>{html.escape(user.username)}</b>.\n"
            "Ссылка действует 30 минут. Если это были не вы — просто проигнорируйте сообщение."
            # кнопка-ссылка возможна только на https; иначе (локальный запуск) — ссылкой в тексте
            + ("" if url.startswith("https://") else f"\n{url}"),
            [[("Задать новый пароль", "url:" + url)]],
        )
    else:
        await asyncio.sleep(0.5)
    return templates.TemplateResponse(request=request, name="forgot.html",
                                      context={"sent": True, "username": username,
                                               "bot_enabled": telegram.enabled() and bool(telegram.bot_link())})


@router.get("/reset", response_class=HTMLResponse)
def reset_page(request: Request, token: str = ""):
    valid = bool(telegram.peek_token("reset", token))
    return templates.TemplateResponse(request=request, name="reset.html", context={"token": token, "valid": valid})


@router.post("/reset")
async def reset(request: Request, token: str = Form(...), password: str = Form(...), password_confirm: str = Form(""), db: AsyncSession = Depends(get_db)):
    if message := password_error(password, password_confirm):
        return templates.TemplateResponse(request=request, name="reset.html",
                                          context={"token": token, "valid": True, "error": message})
    user = await accounts.consume_password_reset(db, token)
    if not user or not user.can_sign_in:
        return templates.TemplateResponse(request=request, name="reset.html", context={"token": "", "valid": False})
    user.password_hash = get_password_hash(password)
    await db.commit()
    await _login_user(request, db, user)
    if user.is_superuser:
        return RedirectResponse(url="/admin/dashboard", status_code=303)
    return RedirectResponse(url="/auth/account?msg=" + "Пароль%20изменён", status_code=303)


# ---------------------------------------------------------------------------
# Вход и привязка через Telegram-бота
# ---------------------------------------------------------------------------
# Вход: сайт создаёт токен и ссылку t.me/<бот>?start=login_<токен>; бот подтверждает,
# что это владелец Telegram, и записывает в токен id аккаунта; страница входа опрашивает /auth/telegram/poll.

@router.get("/telegram", response_class=HTMLResponse)
def telegram_login_page(request: Request, next: str = ""):
    if not (telegram.enabled() and telegram.bot_link()):
        return RedirectResponse(url="/auth/login", status_code=303)
    # В токене — откуда пришёл запрос: бот покажет это и попросит подтвердить вход
    ua = (request.headers.get("user-agent") or "")[:120]
    ip = request.client.host if request.client else "?"
    token = telegram.new_token("login", "?" + json.dumps({"ip": ip, "ua": ua}, ensure_ascii=False))
    request.session["tg_login"] = token
    return templates.TemplateResponse(request=request, name="tg_login.html",
                                      context={"link": telegram.bot_link(f"login_{token}"), "next": _safe_next(next, "/catalog")})


@router.get("/telegram/poll")
async def telegram_login_poll(request: Request, db: AsyncSession = Depends(get_db)):
    token = request.session.get("tg_login")
    value = telegram.peek_token("login", token) if token else None
    if value is None:
        return JSONResponse({"ok": False, "expired": True})
    if not value.isdigit():
        return JSONResponse({"ok": False})
    telegram.take_token("login", token)
    user = (await db.execute(select(User).where(User.id == int(value)))).scalar_one_or_none()
    if not user:
        return JSONResponse({"ok": False, "expired": True})
    await _login_user(request, db, user)
    return JSONResponse({"ok": True})


@router.post("/telegram/link")
async def telegram_link(request: Request, db: AsyncSession = Depends(get_db)):
    """Привязать Telegram к аккаунту: ссылка в бот с одноразовым токеном."""
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/auth/login", status_code=303)
    if not telegram.bot_link():
        return _account_back(error="Telegram-бот не настроен")
    token = telegram.new_token("link", user.id)
    return RedirectResponse(url=telegram.bot_link(f"link_{token}"), status_code=303)


@router.post("/telegram/unlink")
async def telegram_unlink(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/auth/login", status_code=303)
    if not accounts.has_password(user):
        return _account_back(error="Сначала задайте пароль — иначе в аккаунт будет не войти")
    user.telegram_id = None
    await db.commit()
    return _account_back(msg="Telegram отвязан")
