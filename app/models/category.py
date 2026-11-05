from sqlalchemy import Column, Integer, String, ForeignKey
from sqlalchemy.orm import relationship

from app.database import Base


class Category(Base):
    __tablename__ = "categories"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    slug = Column(String(200), nullable=False, unique=True)
    parent_id = Column(Integer, ForeignKey("categories.id"), nullable=True, index=True)
    position = Column(Integer, default=100, nullable=False)  # порядок разделов в меню
    icon = Column(String(16), nullable=True)                 # эмодзи раздела
    onec_id = Column(String(64), nullable=True, unique=True)  # Ид группы в 1С (CommerceML)

    parent = relationship("Category", remote_side=[id], backref="children")
    products = relationship("Product", back_populates="category")

    def __repr__(self):
        return f"<Category {self.name}>"
