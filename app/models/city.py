from sqlalchemy import Column, Integer, String, Boolean
from sqlalchemy.orm import relationship

from app.database import Base


class City(Base):
    __tablename__ = "cities"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False, unique=True)
    slug = Column(String(50), nullable=False, unique=True)
    is_active = Column(Boolean, default=True, nullable=False)
    onec_warehouse_id = Column(String(64), nullable=True)  # Ид склада в 1С (CommerceML)

    stocks = relationship("Stock", back_populates="city")

    def __repr__(self):
        return f"<City {self.name}>"
