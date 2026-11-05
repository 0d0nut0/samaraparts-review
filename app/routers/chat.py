"""Чат на сайте — сторона покупателя (виджет в base.html)."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services import chat, telegram

router = APIRouter(prefix="/chat", tags=["chat"])


@router.get("/state")
async def chat_state(request: Request, after: int = 0, read: int = 0, db: AsyncSession = Depends(get_db)):
    """Новые сообщения диалога (после id=after). read=1 — окно чата открыто, сбрасываем непрочитанные."""
    thread = await chat.find_thread(request, db)
    if not thread:
        return JSONResponse({"thread": None, "messages": [], "unread": 0})
    msgs = await chat.messages_after(db, thread.id, after)
    unread = thread.unread_client
    if read and unread:
        thread.unread_client = 0
        await db.commit()
        unread = 0
    return JSONResponse({
        "thread": {"id": thread.id, "status": thread.status.name, "has_contact": bool(thread.user_id or thread.phone)},
        "messages": [chat.serialize(m) for m in msgs],
        "unread": unread,
    })


@router.post("/send")
async def chat_send(request: Request, db: AsyncSession = Depends(get_db)):
    """Сообщение покупателя. Гость может передать name и phone, чтобы менеджер мог перезвонить."""
    try:
        data = await request.json()
    except ValueError:
        return JSONResponse({"ok": False, "error": "Некорректный запрос"}, status_code=400)
    ip = request.client.host if request.client else "?"
    if chat.rate_limited(("ip", ip), chat.IP_WINDOW, chat.IP_LIMIT):
        return JSONResponse({"ok": False, "error": "Слишком много сообщений — попробуйте позже"}, status_code=429)
    thread = await chat.get_or_create_thread(request, db)
    if chat.rate_limited(thread.id):
        return JSONResponse({"ok": False, "error": "Слишком часто — подождите несколько секунд"}, status_code=429)
    name = str(data.get("name") or "").strip()[:120]
    phone = str(data.get("phone") or "").strip()[:50]
    if name:
        thread.name = name
    if phone:
        thread.phone = phone
    page = str(data.get("page") or "")[:500]
    if page.startswith("/"):
        thread.last_page = page
    first_unread = not thread.unread_staff
    try:
        msg = await chat.add_message(db, thread, "client", str(data.get("text") or ""))
    except chat.ChatError as e:
        await db.rollback()
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    await db.commit()
    if first_unread:
        telegram.fire(telegram.notify_chat_message(thread.id, msg.text))
    return JSONResponse({"ok": True, "message": chat.serialize(msg)})
