from sqlalchemy import Column, Integer, String, ForeignKey, Text, Numeric
from sqlalchemy.orm import relationship

from app.database import Base


class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True)
    sku = Column(String(100), nullable=False, unique=True, index=True)
    name = Column(String(500), nullable=False)
    slug = Column(String(500), nullable=False, index=True)
    description = Column(Text, nullable=True)
    image_url = Column(String(500), nullable=True)

    category_id = Column(Integer, ForeignKey("categories.id"), nullable=False)
    brand = Column(String(100), nullable=True)
    model = Column(String(200), nullable=True, index=True)

    # Цены
    price_moba = Column(Numeric(10, 2), nullable=True)       # цена Мобы (сырая)
    price_base = Column(Numeric(10, 2), nullable=True)       # обычная розничная цена
    price_card = Column(Numeric(10, 2), nullable=True)       # та же розница; старое имя поля
    price_opt = Column(Numeric(10, 2), nullable=True)        # ОПТ = обычная Moba
    price_super_opt = Column(Numeric(10, 2), nullable=True)

    is_active = Column(Integer, default=1, nullable=False)

    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=True)
    critical_stock_threshold = Column(Integer, default=5, nullable=False)
    purchase_price = Column(Numeric(10, 2), nullable=True)

    onec_id = Column(String(80), nullable=True, unique=True, index=True)  # Ид товара в 1С (CommerceML)

    category = relationship("Category", back_populates="products")
    stocks = relationship("Stock", back_populates="product")
    supplier = relationship("Supplier", back_populates="products")

    def __repr__(self):
        return f"<Product {self.sku} {self.name}>"
