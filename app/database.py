"""
Подключение к базе данных (PostgreSQL через SQLAlchemy).
URL берётся из .env переменной DATABASE_URL.

Предоставляет:
  - engine / AsyncSessionLocal — для async-эндпоинтов FastAPI
  - sync_engine / SessionLocal  — для утилитных скриптов (парсер, пересчёт цен)
"""

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import DATABASE_URL

# ---------------------------------------------------------------------------
# Синхронный движок (для Alembic, парсера, пересчёта цен)
# ---------------------------------------------------------------------------
# DATABASE_URL из .env уже имеет вид postgresql+psycopg2://...
sync_db_url = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://")
# Убираем asyncpg-префикс, если вдруг остался
sync_db_url = sync_db_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://")
# Убираем psycopg-async-префикс
sync_db_url = sync_db_url.replace("postgresql+psycopg://", "postgresql+psycopg2://")

sync_engine = create_engine(sync_db_url, echo=False)
SessionLocal = sessionmaker(bind=sync_engine, autocommit=False, autoflush=False)

# ---------------------------------------------------------------------------
# Асинхронный движок (для FastAPI)
# ---------------------------------------------------------------------------
# Для psycopg async URL должен начинаться с postgresql+psycopg://
async_db_url = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://")
async_db_url = async_db_url.replace("postgresql+psycopg2://", "postgresql+psycopg://")
async_db_url = async_db_url.replace("postgresql+asyncpg://", "postgresql+psycopg://")

engine = create_async_engine(async_db_url, echo=False)

# Фабрика асинхронных сессий
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False
)

# Базовый класс для всех ORM-моделей
Base = declarative_base()


async def get_db():
    """
    Асинхронный генератор зависимости FastAPI.
    Гарантирует, что соединение с БД будет закрыто после обработки запроса.
    """
    async with AsyncSessionLocal() as session:
        yield session
