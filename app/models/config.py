from sqlalchemy import Column, String
from app.database import Base

class SystemConfig(Base):
    __tablename__ = "system_config"

    key = Column(String(100), primary_key=True)
    value = Column(String(255), nullable=False)
    description = Column(String(500), nullable=True)
