"""Чат покупателя с менеджером: обращение (диалог) и сообщения."""
import enum

from sqlalchemy import Column, Integer, String, Text, ForeignKey, DateTime, Enum, func
from sqlalchemy.orm import relationship

from app.database import Base


class ChatStatus(enum.Enum):
    OPEN = "open"
    CLOSED = "closed"


class ChatThread(Base):
    __tablename__ = "chat_threads"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    guest_token = Column(String(64), nullable=True, unique=True)  # гость: токен из сессии
    name = Column(String(120), nullable=True)   # как представился гость
    phone = Column(String(50), nullable=True)
    status = Column(Enum(ChatStatus), default=ChatStatus.OPEN, nullable=False)
    assigned_to = Column(Integer, ForeignKey("users.id"), nullable=True)
    unread_staff = Column(Integer, default=0, nullable=False)   # непрочитано менеджером
    unread_client = Column(Integer, default=0, nullable=False)  # непрочитано клиентом
    last_page = Column(String(500), nullable=True)              # где был клиент, когда писал
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    last_message_at = Column(DateTime, server_default=func.now(), nullable=False, index=True)

    user = relationship("User", foreign_keys=[user_id])
    manager = relationship("User", foreign_keys=[assigned_to])
    messages = relationship("ChatMessage", back_populates="thread", order_by="ChatMessage.id",
                            cascade="all, delete-orphan")


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True)
    thread_id = Column(Integer, ForeignKey("chat_threads.id", ondelete="CASCADE"), nullable=False, index=True)
    sender = Column(String(10), nullable=False)  # client | staff | system
    author_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    text = Column(Text, nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    thread = relationship("ChatThread", back_populates="messages")
    author = relationship("User")
