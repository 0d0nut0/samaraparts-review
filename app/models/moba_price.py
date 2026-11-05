from sqlalchemy import Column, Integer, String, Numeric, DateTime, Text, func
from app.database import Base


class MobaPrice(Base):
    __tablename__ = "moba_prices"

    id = Column(Integer, primary_key=True)
    sku = Column(String(100), nullable=False, unique=True, index=True)
    name = Column(String(500), nullable=True)

    # Цены Мобы
    price = Column(Numeric(10, 2), nullable=False)
    discount_price = Column(Numeric(10, 2), nullable=True)

    # Наши расчётные цены (пересчитываются модулем pricing)
    our_base = Column(Numeric(10, 2), nullable=True)   # обычная розница в целых рублях
    our_card = Column(Numeric(10, 2), nullable=True)   # та же розница; старое имя поля
    our_opt = Column(Numeric(10, 2), nullable=True)    # ОПТ = обычная Moba
    our_super_opt = Column(Numeric(10, 2), nullable=True)

    # Дополнительные данные
    image = Column(String(1000), nullable=True)        # ссылка на картинку
    link = Column(String(1000), nullable=True)         # ссылка на карточку на moba.ru
    specifications = Column(Text, nullable=True)       # характеристики (JSON-строка)
    tags = Column(Text, nullable=True)                 # теги (JSON-строка)

    url = Column(String(1000), nullable=True)          # откуда спарсили
    parsed_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def __repr__(self):
        return f"<MobaPrice {self.sku} = {self.price} (disc {self.discount_price})>"
