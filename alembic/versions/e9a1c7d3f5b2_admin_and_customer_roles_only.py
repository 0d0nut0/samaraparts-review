"""Только роли ADMIN, WHOLESALE, RETAIL; уникальный артикул в moba_prices

Роли сотрудников MANAGER и WAREHOUSE убраны: такие пользователи становятся розничными покупателями
(доступ к админке у них пропадает). Дубли артикулов в moba_prices ломали корзину (одна позиция
показывалась дважды), поэтому sku делаем уникальным.

Revision ID: e9a1c7d3f5b2
Revises: d4e8a2b6c1f0
Create Date: 2026-10-28 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = 'e9a1c7d3f5b2'
down_revision: Union[str, Sequence[str], None] = 'd4e8a2b6c1f0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("UPDATE users SET role = 'RETAIL' WHERE role IN ('MANAGER', 'WAREHOUSE')")
    op.execute("ALTER TABLE users ALTER COLUMN role DROP DEFAULT")
    op.execute("ALTER TYPE userrole RENAME TO userrole_old")
    op.execute("CREATE TYPE userrole AS ENUM ('ADMIN', 'WHOLESALE', 'RETAIL')")
    op.execute("ALTER TABLE users ALTER COLUMN role TYPE userrole USING role::text::userrole")
    op.execute("ALTER TABLE users ALTER COLUMN role SET DEFAULT 'RETAIL'")
    op.execute("DROP TYPE userrole_old")

    # Оставляем последнюю запись по каждому артикулу
    op.execute("""
        DELETE FROM moba_prices a USING moba_prices b
        WHERE a.sku = b.sku AND a.id < b.id
    """)
    op.drop_index('ix_moba_prices_sku', table_name='moba_prices', if_exists=True)
    op.create_index('ix_moba_prices_sku', 'moba_prices', ['sku'], unique=True)


def downgrade() -> None:
    op.drop_index('ix_moba_prices_sku', table_name='moba_prices')
    op.create_index('ix_moba_prices_sku', 'moba_prices', ['sku'], unique=False)

    op.execute("ALTER TYPE userrole ADD VALUE IF NOT EXISTS 'MANAGER'")
    op.execute("ALTER TYPE userrole ADD VALUE IF NOT EXISTS 'WAREHOUSE'")
