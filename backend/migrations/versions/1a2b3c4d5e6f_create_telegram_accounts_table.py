"""create telegram accounts table

Revision ID: 1a2b3c4d5e6f
Revises: 8c4c6f75e9ad
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "1a2b3c4d5e6f"
down_revision: Union[str, Sequence[str], None] = "8c4c6f75e9ad"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "telegram_accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("communication_account_id", sa.Integer(), nullable=False),
        sa.Column("phone_number", sa.String(length=30), nullable=True),
        sa.Column("api_id", sa.String(length=100), nullable=True),
        sa.Column("api_hash", sa.String(length=255), nullable=True),
        sa.Column("session_name", sa.String(length=150), nullable=True),
        sa.Column("session_path", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="disconnected"),
        sa.Column("last_login", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["communication_account_id"], ["communication_accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("communication_account_id"),
    )
    op.create_index("ix_telegram_accounts_id", "telegram_accounts", ["id"])
    op.create_index(
        "ix_telegram_accounts_communication_account_id",
        "telegram_accounts",
        ["communication_account_id"],
        unique=True,
    )
    op.create_index("ix_telegram_accounts_phone_number", "telegram_accounts", ["phone_number"])
    op.create_index("ix_telegram_accounts_session_name", "telegram_accounts", ["session_name"])
    op.create_index("ix_telegram_accounts_status", "telegram_accounts", ["status"])


def downgrade():
    op.drop_index("ix_telegram_accounts_status", table_name="telegram_accounts")
    op.drop_index("ix_telegram_accounts_session_name", table_name="telegram_accounts")
    op.drop_index("ix_telegram_accounts_phone_number", table_name="telegram_accounts")
    op.drop_index("ix_telegram_accounts_communication_account_id", table_name="telegram_accounts")
    op.drop_index("ix_telegram_accounts_id", table_name="telegram_accounts")
    op.drop_table("telegram_accounts")
