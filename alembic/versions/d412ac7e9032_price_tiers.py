"""Three explicit price tiers separate from administrator permissions."""
from alembic import op
import sqlalchemy as sa
revision = "d412ac7e9032"
down_revision = "a6c8e2f49031"
branch_labels = depends_on = None

def upgrade():
    op.add_column("products", sa.Column("price_super_opt", sa.Numeric(10, 2)))
    op.add_column("moba_prices", sa.Column("our_super_opt", sa.Numeric(10, 2)))
    op.add_column("users", sa.Column("price_type", sa.String(24)))

def downgrade():
    op.drop_column("users", "price_type")
    op.drop_column("moba_prices", "our_super_opt")
    op.drop_column("products", "price_super_opt")
