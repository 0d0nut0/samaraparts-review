from sqlalchemy import Column, Integer, String, ForeignKey, Numeric, DateTime, Enum, func
from sqlalchemy.orm import relationship
from datetime import datetime
import enum

from app.database import Base

class OrderStatus(enum.Enum):
    PENDING = "Pending"
    PAID = "Paid"
    COMPLETED = "Completed"
    CANCELLED = "Cancelled"

class OrderType(enum.Enum):
    RETAIL = "Retail"
    WHOLESALE = "Wholesale"

class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    city_id = Column(Integer, ForeignKey("cities.id"), nullable=False)
    total_amount = Column(Numeric(10, 2), nullable=False)
    status = Column(Enum(OrderStatus), default=OrderStatus.PENDING, nullable=False)
    order_type = Column(Enum(OrderType), default=OrderType.RETAIL, nullable=False)
    profit = Column(Numeric(10, 2), nullable=True)
    customer_name = Column(String(255), nullable=False)
    customer_phone = Column(String(50), nullable=False)
    address = Column(String(500), nullable=True)
    comment = Column(String(1000), nullable=True)        # комментарий покупателя
    manager_note = Column(String(1000), nullable=True)   # заметка менеджера (покупатель не видит)
    source = Column(String(10), default="site", nullable=False)  # site | bot | admin

    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    onec_exported_at = Column(DateTime, nullable=True)  # когда заказ забрала 1С

    user = relationship("User", back_populates="orders")
    city = relationship("City")
    items = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")

class OrderItem(Base):
    __tablename__ = "order_items"

    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    quantity = Column(Integer, nullable=False)
    price_at_time = Column(Numeric(10, 2), nullable=False)

    order = relationship("Order", back_populates="items")
    product = relationship("Product")
