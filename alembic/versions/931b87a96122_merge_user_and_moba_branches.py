"""merge user and moba branches

Revision ID: 931b87a96122
Revises: 48b427287509, c4f241108103
Create Date: 2026-10-04 13:58:52.608528

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '931b87a96122'
down_revision: Union[str, Sequence[str], None] = ('48b427287509', 'c4f241108103')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
