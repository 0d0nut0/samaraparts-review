"""
Перенос покупателей из базы старого Telegram-бота (SQLite, spare_parts.db) в базу сайта.

После перехода на общий бэкенд бот работает на базе сайта. Чтобы покупатели не потеряли оптовый
статус, их аккаунты создаются заранее: telegram_id, имя, оптовый статус → роль WHOLESALE,
admin → ADMIN. Существующие аккаунты с тем же Telegram не трогаем (только повышаем розницу до опта).
Каталог и заказы старого бота не переносятся: товары и цены теперь общие с сайтом.

Запуск: python -m scripts.import_bot_users путь/к/spare_parts.db [--dry-run]
"""
import argparse
import sqlite3

from sqlalchemy import select

from app.database import SessionLocal
from app.models import User
from app.models.user import UserRole
from app.services.accounts import NO_PASSWORD


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    src = sqlite3.connect(args.db)
    rows = src.execute("SELECT user_id, username, full_name, status, role FROM users").fetchall()
    created = upgraded = skipped = 0
    with SessionLocal() as db:
        for tg_id, username, full_name, status, role in rows:
            if not tg_id or tg_id <= 0:
                continue
            target = UserRole.ADMIN if role == "admin" else UserRole.WHOLESALE if status == "wholesale" else UserRole.RETAIL
            user = db.execute(select(User).where(User.telegram_id == tg_id)).scalar_one_or_none()
            if user:
                if target == UserRole.WHOLESALE and user.role == UserRole.RETAIL:
                    user.role = UserRole.WHOLESALE
                    upgraded += 1
                else:
                    skipped += 1
                continue
            db.add(User(username=f"tg{tg_id}", password_hash=NO_PASSWORD, telegram_id=tg_id,
                        full_name=(full_name or username or None) and (full_name or username)[:200],
                        role=target, is_admin=target == UserRole.ADMIN))
            created += 1
        if args.dry_run:
            db.rollback()
        else:
            db.commit()
    print(f"Создано: {created}, повышено до опта: {upgraded}, без изменений: {skipped}" + (" (dry-run)" if args.dry_run else ""))


if __name__ == "__main__":
    main()
