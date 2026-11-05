"""
Точка входа приложения Parts.Site.
Здесь собираются все роутеры, подключаются middleware и задаётся порядок их исполнения.
"""

import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import select

from app.config import SECRET_KEY, HTTPS_ONLY, settings
from app.database import AsyncSessionLocal
from app.models.city import City
from app.models.user import User
from app.services import accounts
from app import seo
from app.routers import auth, cart, catalog, admin, chat, checkout, onec, pages, tg
from app.templating import templates
from app.shop_info import SHOP

@asynccontextmanager
async def lifespan(_: FastAPI):
    # psycopg (async) не работает на ProactorEventLoop — он по умолчанию у uvicorn на Windows без --reload
    if sys.platform == "win32" and isinstance(asyncio.get_running_loop(), asyncio.ProactorEventLoop):
        raise RuntimeError(
            "Windows: запустите uvicorn с --loop asyncio:SelectorEventLoop (или с --reload) — "
            "psycopg async несовместим с ProactorEventLoop"
        )
    yield


# Автодокументация API (/docs, /openapi.json) раскрыла бы все адреса админки — выключена
app = FastAPI(title="ПАРТС", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.exception_handler(StarletteHTTPException)
async def html_error_page(request: Request, exc: StarletteHTTPException):
    """403/404 для браузера — страница в стиле сайта, для API — стандартный JSON."""
    if exc.status_code in (403, 404) and "text/html" in request.headers.get("accept", ""):
        titles = {403: "Доступ запрещён", 404: "Страница не найдена"}
        detail = exc.detail if exc.detail not in ("Not Found", "Forbidden") else None
        return templates.TemplateResponse(
            request=request,
            name="404.html",
            context={"code": exc.status_code, "title": titles[exc.status_code], "detail": detail},
            status_code=exc.status_code,
        )
    return await http_exception_handler(request, exc)

# ---------------------------------------------------------------------------
# Middleware
# ВАЖНО: FastAPI (Starlette) применяет middleware в ОБРАТНОМ порядке
# относительно add_middleware-вызовов.
# Чтобы наш код имел доступ к request.session, он должен выполняться
# ПОСЛЕ SessionMiddleware → значит add_middleware(SessionMiddleware) идёт ПОСЛЕДНИМ.
# ---------------------------------------------------------------------------

@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    """
    Добавляем HTTP-заголовки безопасности (OWASP рекомендации).
    """
    if Path(settings.MAINTENANCE_FILE).is_file() and not request.url.path.startswith(('/auth/', '/static/', '/favicon')):
        return HTMLResponse('<!doctype html><html lang="ru"><meta charset="utf-8"><title>Обновляем каталог — ПАРТС</title><body style="font:18px Arial;max-width:600px;margin:15vh auto;padding:24px;color:#004c35"><h1>Обновляем каталог</h1><p>Загружаем актуальные товары и наличие из 1С. Пожалуйста, зайдите немного позже.</p></body></html>', status_code=503,
                            headers={'Retry-After':'60','Cache-Control':'no-store'})
    if HTTPS_ONLY and request.url.scheme == "http" and request.url.hostname not in ("127.0.0.1", "localhost", "testserver"):
        return RedirectResponse(str(request.url.replace(scheme="https")), status_code=308)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    if HTTPS_ONLY:
        # HSTS имеет смысл только за HTTPS, иначе на localhost браузер «залипнет» на https
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    # Всё своё (Tailwind, HTMX, шрифты — /static). telegram.org — скрипт Mini App (/tg/app).
    # Встраивать сайт во фрейм можно только Telegram Web (Mini App в браузерной версии Telegram);
    # frame-ancestors заменяет X-Frame-Options.
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "font-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://telegram.org; "
        "img-src 'self' data: https:; "
        "connect-src 'self'; "
        "frame-ancestors 'self' https://web.telegram.org https://*.telegram.org;"
    )
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
    if request.url.path.startswith(("/admin", "/auth", "/checkout", "/cart", "/chat", "/tg")):
        response.headers["Cache-Control"] = "no-store"  # личные страницы не кэшируются браузером и прокси
    return response

@app.middleware("http")
async def add_user_to_request(request: Request, call_next):
    """
    Инжектируем текущего пользователя в request.state.user перед каждым запросом.
    Шаблоны могут читать {{ request.state.user }} без дополнительных зависимостей.
    """
    request.state.user = None
    user_id = request.session.get("user_id")

    async with AsyncSessionLocal() as db:
        if user_id:
            result = await db.execute(select(User).where(User.id == user_id))
            request.state.user = result.scalar_one_or_none()
            if request.state.user is not None and (not request.state.user.can_sign_in or request.session.get("pwv") != accounts.session_fingerprint(request.state.user)):
                request.state.user = None  # пароль сменили после входа (или сессия до этой проверки) — входим заново
            if request.state.user is None:
                # Пользователь удалён — забываем его в сессии, иначе корзина пишет в несуществующий аккаунт
                request.session.pop("user_id", None)
        # Город покупателя (выбор в шапке): по нему показываем наличие и предлагаем склад при заказе
        cities = await _active_cities(db)
    request.state.cities = cities
    request.state.city = next((c for c in cities if c.id == request.session.get("city_id")), cities[0] if cities else None)

    return await call_next(request)


_cities_cache: tuple[float, list] = (0.0, [])


async def _active_cities(db) -> list:
    """Активные города с кэшем на 5 минут — список нужен в шапке на каждой странице."""
    global _cities_cache
    if time.monotonic() - _cities_cache[0] > 300:
        rows = (await db.execute(select(City).where(City.is_active.is_(True)).order_by(City.id))).scalars().all()
        order = {branch["name"]: i for i, branch in enumerate(SHOP["branches"])}
        rows.sort(key=lambda city: (order.get(city.name, len(order)), city.id))
        _cities_cache = (time.monotonic(), [SimpleNamespace(id=c.id, name=c.name) for c in rows])
    return _cities_cache[1]


# SessionMiddleware добавляем ПОСЛЕДНИМ — она должна обернуть всё, включая
# наш add_user_to_request middleware, чтобы request.session был доступен.
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    max_age=60 * 60 * 24 * 14,   # 2 недели
    same_site="lax",
    https_only=HTTPS_ONLY,       # True на продакшне за HTTPS
)

# ---------------------------------------------------------------------------
# Роутеры
# ---------------------------------------------------------------------------

@app.get("/")
def root():
    """Перенаправляет с корня на каталог."""
    return RedirectResponse(url="/catalog")


@app.post("/city")
async def choose_city(request: Request):
    """Выбор города в шапке: запоминаем в сессии и возвращаем на ту же страницу."""
    form = await request.form()
    try:
        request.session["city_id"] = int(form.get("city_id", ""))
    except ValueError:
        pass
    back = request.headers.get("referer") or "/catalog"
    return RedirectResponse(url=back if back.startswith(str(request.base_url)) else "/catalog", status_code=303)


@app.get("/health")
def health():
    """Health-check для мониторинга."""
    return {"status": "ok"}


# Фото товаров (scripts/attach_photos.py кладёт их в media/products)
MEDIA_DIR = Path(__file__).resolve().parent.parent / "media"
MEDIA_DIR.mkdir(exist_ok=True)
app.mount("/media", StaticFiles(directory=MEDIA_DIR), name="media")
# Tailwind и HTMX раздаём сами: внешние CDN бывают недоступны из сети магазина
app.mount("/static", StaticFiles(directory=Path(__file__).resolve().parent / "static"), name="static")

app.include_router(catalog.router)
app.include_router(cart.router)
app.include_router(checkout.router)
app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(pages.router)
app.include_router(chat.router)
app.include_router(onec.router)
app.include_router(seo.router)
app.include_router(tg.router)
