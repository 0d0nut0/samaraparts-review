"""SMTP и одноразовые ссылки подтверждения. В БД хранится только SHA-256 токена."""
import asyncio
import hashlib
import html
import logging
import re
import secrets
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from urllib.parse import urlencode

from sqlalchemy import select, update

from app.config import settings
from app.models.user import User

logger = logging.getLogger(__name__)
TOKEN_LIFETIME = timedelta(hours=24)


class MailError(Exception):
    pass


def configured() -> bool:
    return bool(settings.SMTP_HOST and settings.SMTP_FROM)


def normalize_email(value: str) -> str | None:
    value = value.strip().lower()
    if len(value) > 254 or value.count("@") != 1:
        return None
    local, domain = value.split("@")
    if not local or len(local) > 64 or local.startswith(".") or local.endswith(".") or ".." in local:
        return None
    if not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+", local):
        return None
    labels = domain.split(".")
    if len(labels) < 2 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels):
        return None
    return value


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _token_conditions(token: str):
    return (User.email_token_hash == token_hash(token), User.email_verified.is_(False),
            User.email_token_expires_at > datetime.now(timezone.utc))


async def valid_token(db, token: str) -> bool:
    if not token or len(token) > 128:
        return False
    return (await db.execute(select(User.id).where(*_token_conditions(token)))).scalar_one_or_none() is not None


async def confirm(db, token: str) -> bool:
    if not token or len(token) > 128:
        return False
    # Условие и изменение атомарны: параллельный повтор не использует ссылку второй раз.
    result = await db.execute(update(User).where(*_token_conditions(token)).values(
        email_verified=True, email_token_hash=None, email_token_expires_at=None).returning(User.id))
    confirmed = result.scalar_one_or_none() is not None
    await db.commit()
    return confirmed


def _send(recipient: str, username: str, url: str) -> None:
    message = EmailMessage()
    message["Subject"] = "Подтвердите регистрацию на ПАРТС"
    message["From"] = settings.SMTP_FROM
    message["To"] = recipient
    message.set_content(f"Здравствуйте, {username}!\n\nПодтвердите регистрацию на ПАРТС:\n{url}\n\n"
                        "Ссылка действует 24 часа и используется один раз.\n"
                        "Если Вы не регистрировались, проигнорируйте это письмо.")
    message.add_alternative(
        '<!doctype html><html lang="ru"><body style="margin:0;background:#f3f7f3;'
        'font-family:Arial,sans-serif;color:#173d30;padding:32px 16px">'
        '<div style="max-width:480px;margin:0 auto;background:white;border:1px solid #dce7df;'
        'border-radius:18px;padding:32px">'
        '<div style="font-size:28px;font-weight:bold;color:#005038">ПАРТС</div>'
        f'<h1 style="font-size:22px;margin-top:28px">Здравствуйте, {html.escape(username)}!</h1>'
        '<p style="line-height:1.6">Подтвердите Вашу почту, чтобы завершить регистрацию.</p>'
        f'<a href="{html.escape(url, quote=True)}" style="display:inline-block;margin:16px 0;'
        'background:#005038;color:#fff;padding:16px 28px;border-radius:10px;'
        'text-decoration:none;font-weight:bold">ПОДТВЕРДИТЬ</a>'
        '<p style="font-size:13px;line-height:1.6;color:#65786d">Ссылка действует 24 часа. '
        'Если Вы не регистрировались, проигнорируйте это письмо.</p>'
        '</div></body></html>', subtype="html")
    context = ssl.create_default_context()
    kwargs = {"host": settings.SMTP_HOST, "port": settings.SMTP_PORT, "timeout": settings.SMTP_TIMEOUT}
    client = smtplib.SMTP_SSL(context=context, **kwargs) if settings.SMTP_SSL else smtplib.SMTP(**kwargs)
    with client as smtp:
        if not settings.SMTP_SSL and settings.SMTP_STARTTLS:
            smtp.starttls(context=context)
        if settings.SMTP_USERNAME:
            smtp.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
        smtp.send_message(message)


async def send_verification(db, user: User, next_path: str = "") -> None:
    if not configured():
        raise MailError("Почта временно недоступна. Попробуйте позже.")
    token = secrets.token_urlsafe(32)
    user.email_token_hash = token_hash(token)
    user.email_token_expires_at = datetime.now(timezone.utc) + TOKEN_LIFETIME
    await db.commit()
    query = urlencode({"token": token, "next": next_path})
    url = settings.SITE_URL.rstrip("/") + "/auth/verify-email?" + query
    try:
        await asyncio.to_thread(_send, user.email, user.username, url)
    except (OSError, ValueError, smtplib.SMTPException):
        # Не пишем в журнал адрес, пароль SMTP, ссылку или тело письма.
        logger.error("Registration verification email delivery failed")
        raise MailError("Не удалось отправить письмо. Запросите его повторно немного позже.") from None
