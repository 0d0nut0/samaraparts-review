"""chat threads and messages

Revision ID: d4e8a2b6c1f0
Revises: c3a9f1d2e7b4
Create Date: 2026-10-27 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd4e8a2b6c1f0'
down_revision: Union[str, Sequence[str], None] = 'c3a9f1d2e7b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'chat_threads',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('guest_token', sa.String(length=64), nullable=True, unique=True),
        sa.Column('name', sa.String(length=120), nullable=True),
        sa.Column('phone', sa.String(length=50), nullable=True),
        sa.Column('status', sa.Enum('OPEN', 'CLOSED', name='chatstatus'), nullable=False, server_default='OPEN'),
        sa.Column('assigned_to', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('unread_staff', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('unread_client', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_page', sa.String(length=500), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('last_message_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
    )
    op.create_index('ix_chat_threads_user_id', 'chat_threads', ['user_id'])
    op.create_index('ix_chat_threads_last_message_at', 'chat_threads', ['last_message_at'])
    op.create_table(
        'chat_messages',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('thread_id', sa.Integer(), sa.ForeignKey('chat_threads.id', ondelete='CASCADE'), nullable=False),
        sa.Column('sender', sa.String(length=10), nullable=False),
        sa.Column('author_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
    )
    op.create_index('ix_chat_messages_thread_id', 'chat_messages', ['thread_id'])


def downgrade() -> None:
    op.drop_index('ix_chat_messages_thread_id', table_name='chat_messages')
    op.drop_table('chat_messages')
    op.drop_index('ix_chat_threads_last_message_at', table_name='chat_threads')
    op.drop_index('ix_chat_threads_user_id', table_name='chat_threads')
    op.drop_table('chat_threads')
    sa.Enum(name='chatstatus').drop(op.get_bind(), checkfirst=True)
