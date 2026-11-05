from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, Enum, func, Text
from sqlalchemy.orm import relationship
import enum

from app.database import Base

class ApplicationStatus(enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"

class WholesaleApplication(Base):
    __tablename__ = "wholesale_applications"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    company_details = Column(Text, nullable=False)
    status = Column(Enum(ApplicationStatus), default=ApplicationStatus.PENDING, nullable=False)
    
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, onupdate=func.now())

    user = relationship("User")

    def __repr__(self):
        return f"<WholesaleApplication user_id={self.user_id} status={self.status}>"
