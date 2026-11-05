"""
Подготовка при запуске контейнера (deploy/entrypoint.sh), безопасно повторять:

1. ADMIN_ID / ADMIN_IDS (Telegram id через запятую) — эти люди становятся администраторами.
   Если аккаунта с таким Telegram нет, он создаётся (логин tg<id>, без пароля): администратор входит на сайт
   кнопкой «Войти через Telegram» и при желании задаёт пароль в кабинете.
2. Если в $DATA_DIR лежит база старого бота (spare_parts.db) — один раз переносим оттуда покупателей
   с оптовым статусом (scripts/import_bot_users.py) и запоминаем, что перенос сделан.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

from sqlalchemy import select

from app.database import SessionLocal
from app.models import User
from app.models.user import UserRole
from app.services.accounts import NO_PASSWORD


def ensure_admins() -> None:
    ids = {int(x) for x in re.findall(r"\d+", ",".join(filter(None, (os.getenv("ADMIN_IDS"), os.getenv("ADMIN_ID")))))}
    if not ids:
        return
    with SessionLocal() as db:
        for tg_id in ids:
            user = db.execute(select(User).where(User.telegram_id == tg_id)).scalar_one_or_none()
            if not user:
                user = User(username=f"tg{tg_id}", password_hash=NO_PASSWORD, telegram_id=tg_id)
                db.add(user)
            if user.role != UserRole.ADMIN or not user.is_admin:
                user.role, user.is_admin = UserRole.ADMIN, True
                print(f"Администратор: Telegram {tg_id} ({user.username})")
        db.commit()


def import_old_bot() -> None:
    data = Path(os.getenv("DATA_DIR", "/app/data"))
    old, mark = data / "spare_parts.db", data / ".bot_users_imported"
    if old.exists() and old.stat().st_size > 0 and not mark.exists():
        result = subprocess.run([sys.executable, "-m", "scripts.import_bot_users", str(old)])
        if result.returncode == 0:
            mark.write_text("ok\n")


if __name__ == "__main__":
    import_old_bot()
    ensure_admins()
