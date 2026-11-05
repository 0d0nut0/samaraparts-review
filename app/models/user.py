"""
Модель пользователя.
Хранит логин и хеш пароля (PBKDF2-SHA256, реализован в app/routers/auth.py).
"""

from sqlalchemy import BigInteger, Column, Integer, String, Boolean, Enum, DateTime
from sqlalchemy.orm import relationship
import enum
from app.database import Base

class UserRole(str, enum.Enum):
    """ADMIN — доступ к админке; WHOLESALE и RETAIL — покупатели (оптовые и розничные цены)."""
    ADMIN = "admin"
    WHOLESALE = "wholesale"
    RETAIL = "retail"


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)

    # Уникальное имя пользователя — используется как логин
    username = Column(String, unique=True, index=True, nullable=False)

    # Хеш вида "pbkdf2_sha256$<соль>$<хеш>" — пароль никогда не хранится в открытом виде
    password_hash = Column(String, nullable=False)

    is_admin = Column(Boolean, default=False, nullable=False)
    role = Column(Enum(UserRole), default=UserRole.RETAIL, nullable=False)

    # Привязанный Telegram: вход и восстановление пароля через бота, уведомления о заказах.
    # Покупатель, пришедший из бота, получает аккаунт автоматически (username «tg<id>»).
    telegram_id = Column(BigInteger, unique=True, nullable=True, index=True)
    full_name = Column(String(200), nullable=True)
    phone = Column(String(50), nullable=True)
    price_type = Column(String(24), nullable=True)

    @property
    def effective_price_type(self):
        return self.price_type or ("wholesale" if self.is_wholesale else "retail")

    email = Column(String(254), unique=True, nullable=True, index=True)
    email_verified = Column(Boolean, default=False, server_default="false", nullable=False)
    email_token_hash = Column(String(64), unique=True, nullable=True)
    email_token_expires_at = Column(DateTime(timezone=True), nullable=True)

    @property
    def can_sign_in(self) -> bool:
        # Существующие аккаунты и вход через Telegram без e-mail продолжают работать.
        return not self.email or bool(self.email_verified)

    @property
    def is_superuser(self) -> bool:
        """Полный доступ к админке: флаг is_admin или роль ADMIN."""
        return bool(self.is_admin) or self.role == UserRole.ADMIN

    @property
    def is_wholesale(self) -> bool:
        """Покупает по оптовым ценам."""
        if self.price_type in ("retail", "wholesale", "super_wholesale"):
            return self.price_type != "retail"
        return self.role == UserRole.WHOLESALE

    def __repr__(self):
        return f"<User id={self.id} username={self.username!r}>"

    orders = relationship("Order", back_populates="user")
    cart_items = relationship("UserCart", back_populates="user")
