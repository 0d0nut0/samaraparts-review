"""Email and expiring single-use registration verification tokens."""
from alembic import op
import sqlalchemy as sa

revision = "a6c8e2f49031"
down_revision = "f2b8d4a6c9e1"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("email", sa.String(254), nullable=True))
    op.add_column("users", sa.Column("email_verified", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("users", sa.Column("email_token_hash", sa.String(64), nullable=True))
    op.add_column("users", sa.Column("email_token_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_unique_constraint("uq_users_email_token_hash", "users", ["email_token_hash"])


def downgrade():
    op.drop_constraint("uq_users_email_token_hash", "users", type_="unique")
    op.drop_index("ix_users_email", table_name="users")
    for name in ("email_token_expires_at", "email_token_hash", "email_verified", "email"):
        op.drop_column("users", name)
