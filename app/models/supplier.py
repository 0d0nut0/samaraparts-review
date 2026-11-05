from sqlalchemy import Column, Integer, String, Text, Numeric, Float
from sqlalchemy.orm import relationship

from app.database import Base

class Supplier(Base):
    __tablename__ = "suppliers"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    contact_info = Column(Text, nullable=True)
    average_delivery_days = Column(Integer, default=0)
    rating = Column(Float, default=5.0)

    products = relationship("Product", back_populates="supplier")

    def __repr__(self):
        return f"<Supplier {self.name}>"
