"""Telegram у пользователя (вход, восстановление пароля, уведомления); комментарии и источник заказа

Revision ID: f2b8d4a6c9e1
Revises: e9a1c7d3f5b2
Create Date: 2026-10-28 20:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = 'f2b8d4a6c9e1'
down_revision: Union[str, Sequence[str], None] = 'e9a1c7d3f5b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('telegram_id', sa.BigInteger(), nullable=True))
    op.add_column('users', sa.Column('full_name', sa.String(length=200), nullable=True))
    op.add_column('users', sa.Column('phone', sa.String(length=50), nullable=True))
    op.create_index('ix_users_telegram_id', 'users', ['telegram_id'], unique=True)

    op.add_column('orders', sa.Column('comment', sa.String(length=1000), nullable=True))
    op.add_column('orders', sa.Column('manager_note', sa.String(length=1000), nullable=True))
    op.add_column('orders', sa.Column('source', sa.String(length=10), nullable=False, server_default='site'))


def downgrade() -> None:
    op.drop_column('orders', 'source')
    op.drop_column('orders', 'manager_note')
    op.drop_column('orders', 'comment')
    op.drop_index('ix_users_telegram_id', table_name='users')
    op.drop_column('users', 'phone')
    op.drop_column('users', 'full_name')
    op.drop_column('users', 'telegram_id')
