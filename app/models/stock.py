from sqlalchemy import Column, Integer, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship

from app.database import Base


class Stock(Base):
    __tablename__ = "stocks"
    __table_args__ = (
        UniqueConstraint("product_id", "city_id", name="uq_product_city"),
    )

    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    city_id = Column(Integer, ForeignKey("cities.id"), nullable=False)
    quantity = Column(Integer, default=0, nullable=False)

    product = relationship("Product", back_populates="stocks")
    city = relationship("City", back_populates="stocks")

    def __repr__(self):
        return f"<Stock product={self.product_id} city={self.city_id} qty={self.quantity}>"
