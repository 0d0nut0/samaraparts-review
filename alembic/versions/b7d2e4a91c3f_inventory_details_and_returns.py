"""inventory details (supplier, unit cost, notes) and RETURN/ADJUSTMENT types

Revision ID: b7d2e4a91c3f
Revises: 570f11667b2f
Create Date: 2026-10-26 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b7d2e4a91c3f'
down_revision: Union[str, Sequence[str], None] = '570f11667b2f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # ADD VALUE нельзя использовать в той же транзакции, поэтому — autocommit-блок
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE transactiontype ADD VALUE IF NOT EXISTS 'RETURN'")
        op.execute("ALTER TYPE transactiontype ADD VALUE IF NOT EXISTS 'ADJUSTMENT'")

    op.add_column('inventory_transactions', sa.Column('supplier_id', sa.Integer(), nullable=True))
    op.add_column('inventory_transactions', sa.Column('unit_cost', sa.Numeric(precision=10, scale=2), nullable=True))
    op.add_column('inventory_transactions', sa.Column('notes', sa.Text(), nullable=True))
    op.create_foreign_key(
        'fk_inventory_transactions_supplier_id', 'inventory_transactions', 'suppliers', ['supplier_id'], ['id']
    )
    op.create_index('ix_inventory_transactions_created_at', 'inventory_transactions', ['created_at'])


def downgrade() -> None:
    """Downgrade schema (значения enum в PostgreSQL удалить нельзя — они остаются)."""
    op.drop_index('ix_inventory_transactions_created_at', table_name='inventory_transactions')
    op.drop_constraint('fk_inventory_transactions_supplier_id', 'inventory_transactions', type_='foreignkey')
    op.drop_column('inventory_transactions', 'notes')
    op.drop_column('inventory_transactions', 'unit_cost')
    op.drop_column('inventory_transactions', 'supplier_id')
