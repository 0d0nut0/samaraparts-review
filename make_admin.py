import asyncio
import sys

from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models.user import User

async def main():
    if len(sys.argv) < 2:
        print("Использование: python make_admin.py <имя_пользователя>")
        sys.exit(1)

    username = sys.argv[1]

    async with AsyncSessionLocal() as session:
        # Ищем пользователя в базе
        result = await session.execute(select(User).where(User.username == username))
        user = result.scalar_one_or_none()

        if not user:
            print(f"Ошибка: Пользователь '{username}' не найден.")
            sys.exit(1)

        from app.models.user import UserRole
        if user.is_admin and user.role == UserRole.ADMIN:
            print(f"Пользователь '{username}' уже является администратором.")
            sys.exit(0)

        # Выдаем права
        user.is_admin = True
        user.role = UserRole.ADMIN
        await session.commit()
        print(f"Успех! Права администратора успешно выданы пользователю '{username}'.")

if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        
    asyncio.run(main())
