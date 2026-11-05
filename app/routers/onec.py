"""
Эндпоинт обмена с 1С: /1c_exchange (протокол «Обмен с сайтом», CommerceML 2).

Последовательность со стороны 1С:
  catalog: checkauth → init → file (import.xml, offers.xml, картинки) → import (по каждому XML)
  sale:    checkauth → init → query (заказы сайта) → success (подтверждение получения) [→ file]

Выключен, пока ONEC_EXCHANGE_ENABLED=false (отвечает 404). Сессии обмена хранятся в памяти процесса,
поэтому сайт должен работать в одном процессе uvicorn (или вынесите сессии в Redis).
"""
import base64
import hmac
import logging
import os
import shutil
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.services import onec_exchange

logger = logging.getLogger(__name__)
router = APIRouter(tags=["1c"])

COOKIE_NAME = "onec_exchange"
SESSION_TTL = 3600
_sessions: dict[str, float] = {}
_last_sent_orders: dict[str, list[int]] = {}


def _text(body: str, status: int = 200) -> PlainTextResponse:
    return PlainTextResponse(body, status_code=status)


def _upload_dir(token: str = "") -> Path:
    path = Path(settings.ONEC_UPLOAD_DIR).resolve()
    if token:
        path = path / token
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_path(filename: str, token: str = "") -> Path | None:
    """Файл внутри каталога обмена (1С присылает и подпапки import_files/…); выход за пределы запрещён."""
    base = _upload_dir(token)
    target = (base / filename.replace("\\", "/").lstrip("/")).resolve()
    return target if target.is_relative_to(base) and target != base else None


def _check_basic_auth(request: Request) -> bool:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic ") or not settings.ONEC_EXCHANGE_PASSWORD:
        return False
    try:
        login, _, password = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    return hmac.compare_digest(login, settings.ONEC_EXCHANGE_LOGIN) and hmac.compare_digest(
        password, settings.ONEC_EXCHANGE_PASSWORD
    )


def _session_ok(request: Request) -> bool:
    token = request.cookies.get(COOKIE_NAME, "")
    expires = _sessions.get(token)
    return bool(expires and expires > time.time())


@router.api_route("/1c_exchange", methods=["GET", "POST"])
@router.api_route("/1c_exchange.php", methods=["GET", "POST"], include_in_schema=False)
async def exchange(request: Request, type: str = "", mode: str = "", filename: str = "",
                   db: AsyncSession = Depends(get_db)) -> Response:
    if not settings.ONEC_EXCHANGE_ENABLED:
        return _text("failure\nОбмен с 1С выключен (ONEC_EXCHANGE_ENABLED=false)", 404)

    if mode == "checkauth":
        if not _check_basic_auth(request):
            logger.warning("1С: неверный логин/пароль обмена с %s", request.client.host if request.client else "?")
            return _text("failure\nНеверный логин или пароль", 401)
        token = onec_exchange.new_session_token()
        now = time.time()
        for t, exp in list(_sessions.items()):
            if exp < now:
                _sessions.pop(t, None)
                _last_sent_orders.pop(t, None)
                shutil.rmtree(_upload_dir(t), ignore_errors=True)
        _sessions[token] = now + SESSION_TTL
        return _text(f"success\n{COOKIE_NAME}\n{token}")

    if not _session_ok(request):
        return _text("failure\nНе выполнена авторизация (checkauth)", 401)

    token = request.cookies.get(COOKIE_NAME, "")

    if mode == "init":
        if type == "catalog":
            shutil.rmtree(_upload_dir(token), ignore_errors=True)  # новая выгрузка — старые файлы не нужны
        return _text(f"zip=no\nfile_limit={settings.ONEC_FILE_LIMIT}")

    if mode == "file":
        target = _safe_path(filename, token)
        if not target:
            return _text("failure\nНедопустимое имя файла", 400)
        target.parent.mkdir(parents=True, exist_ok=True)
        used = sum(p.stat().st_size for p in _upload_dir(token).rglob("*") if p.is_file())
        with open(target, "ab") as f:  # файл может приходить частями
            async for chunk in request.stream():
                used += len(chunk)
                if used > settings.ONEC_FILE_LIMIT:
                    return _text("failure\nПревышен лимит размера выгрузки", 413)
                f.write(chunk)
        return _text("success")

    if type == "catalog" and mode == "import":
        target = _safe_path(filename, token)
        if not target or not target.is_file():
            return _text(f"failure\nФайл {filename} не загружен", 400)
        try:
            data = target.read_bytes()
            name = os.path.basename(filename).lower()
            if name.startswith("offers") or name.startswith("prices") or name.startswith("rests"):
                result = await onec_exchange.import_offers(db, data)
            else:
                result = await onec_exchange.import_catalog(db, data)
            # New 1C products are matched to the latest complete public snapshot.
            # A failed refresh does not turn a supplier cost into a selling price.
            from datetime import datetime, timezone, timedelta
            import json
            from app.services.catalog_sync import sync_snapshot
            cached = Path(settings.MOBA_SNAPSHOT_PATH)
            if cached.is_file():
                snapshot = json.loads(cached.read_text())
                stamp = datetime.fromisoformat(snapshot['fetched_at'])
                if datetime.now(timezone.utc) - stamp < timedelta(hours=24):
                    await db.run_sync(lambda sync: sync_snapshot(sync, snapshot, apply=True))
            await db.commit()
        except Exception as e:
            await db.rollback()
            logger.exception("1С: ошибка импорта %s", filename)
            return _text(f"failure\n{e.__class__.__name__}: {e}")  # type здесь — параметр запроса
        logger.info("1С: импорт %s — %s", filename, result)
        return _text(f"success\n{result}")

    if type == "sale" and mode == "query":
        orders = await onec_exchange.pending_orders(db)
        _last_sent_orders[token] = [o.id for o in orders]
        return Response(onec_exchange.orders_xml(orders), media_type="application/xml; charset=utf-8")

    if type == "sale" and mode == "success":
        await onec_exchange.mark_exported(db, _last_sent_orders.get(token, []))
        await db.commit()
        logger.info("1С: подтверждено получение заказов %s", _last_sent_orders.get(token, []))
        _last_sent_orders.pop(token, None)
        return _text("success")

    return _text(f"failure\nНеизвестный режим type={type} mode={mode}", 400)


@router.get('/1c/prices')
async def calculated_prices(request: Request, db: AsyncSession = Depends(get_db)):
    from fastapi.responses import JSONResponse
    if not settings.ONEC_EXCHANGE_ENABLED:
        return _text('Обмен выключен', 404)
    if not _check_basic_auth(request):
        return _text('Требуется авторизация обмена', 401)
    return JSONResponse(await onec_exchange.price_feed(db), headers={'Cache-Control': 'no-store'})
