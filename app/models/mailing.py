from sqlalchemy import Column, Integer, String, Text, DateTime, Enum, func
import enum

from app.database import Base

class MailingStatus(enum.Enum):
    DRAFT = "draft"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"

class Mailing(Base):
    __tablename__ = "mailings"

    id = Column(Integer, primary_key=True)
    subject = Column(String(255), nullable=False)
    body_template = Column(Text, nullable=False)
    target_segment = Column(String(100), nullable=False) # e.g., 'all', 'wholesale', 'active'
    status = Column(Enum(MailingStatus), default=MailingStatus.DRAFT, nullable=False)
    
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    sent_at = Column(DateTime, nullable=True)

    def __repr__(self):
        return f"<Mailing {self.subject} status={self.status}>"
