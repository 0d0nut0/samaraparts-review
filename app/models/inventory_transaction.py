from sqlalchemy import Column, Integer, ForeignKey, DateTime, Numeric, Enum, Text, func
from sqlalchemy.orm import relationship
import enum

from app.database import Base

class TransactionType(enum.Enum):
    RECEIPT = "receipt"      # приход партии
    WRITE_OFF = "write_off"  # списание (брак, инвентаризация)
    TRANSFER = "transfer"    # перемещение между складами
    SALE = "sale"            # продажа по заказу
    RETURN = "return"        # возврат на склад (отмена заказа)
    ADJUSTMENT = "adjustment"  # ручная корректировка остатка

class InventoryTransaction(Base):
    """Движение товара. quantity — знаковая дельта: приход > 0, расход < 0."""
    __tablename__ = "inventory_transactions"

    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True) # Кто совершил
    type = Column(Enum(TransactionType), nullable=False)
    quantity = Column(Integer, nullable=False)

    from_city_id = Column(Integer, ForeignKey("cities.id"), nullable=True)
    to_city_id = Column(Integer, ForeignKey("cities.id"), nullable=True)

    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=True)
    unit_cost = Column(Numeric(10, 2), nullable=True)  # закупочная цена за шт. (для прихода)
    notes = Column(Text, nullable=True)

    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    product = relationship("Product")
    user = relationship("User")
    supplier = relationship("Supplier")
    from_city = relationship("City", foreign_keys=[from_city_id])
    to_city = relationship("City", foreign_keys=[to_city_id])

    def __repr__(self):
        return f"<InventoryTransaction {self.type} product_id={self.product_id} qty={self.quantity}>"
