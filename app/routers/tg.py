"""
Mini App = сам сайт. Кнопка «Магазин» в боте открывает /tg/app: страница берёт подписанный
Telegram.WebApp.initData, сервер проверяет подпись, входит в аккаунт этого Telegram (или создаёт его)
и открывает каталог. Дальше — обычный сайт: тот же дизайн, поиск, корзина и цены.
"""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.routers.auth import _login_user
from app.services import accounts
from app.templating import templates

router = APIRouter(prefix="/tg", tags=["telegram"])


@router.get("/app", response_class=HTMLResponse)
def mini_app(request: Request):
    return templates.TemplateResponse(request=request, name="tg_app.html", context={})


@router.post("/auth")
async def mini_app_auth(request: Request, db: AsyncSession = Depends(get_db)):
    try:
        data = await request.json()
    except ValueError:
        data = {}
    tg = accounts.check_webapp_init_data(str(data.get("init_data") or ""))
    if not tg:
        return JSONResponse({"ok": False, "error": "Откройте магазин из Telegram-бота"}, status_code=403)
    current = request.session.get("user_id")
    user = await accounts.telegram_user(db, tg["id"], " ".join(filter(None, (tg.get("first_name"), tg.get("last_name")))),
                                        tg.get("username") or "")
    if current != user.id:
        await _login_user(request, db, user)
    return JSONResponse({"ok": True})
