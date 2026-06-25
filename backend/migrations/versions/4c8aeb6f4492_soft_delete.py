"""soft delete

Revision ID: 4c8aeb6f4492
Revises: fe7b836a29a6
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "4c8aeb6f4492"
down_revision: Union[str, Sequence[str], None] = "fe7b836a29a6"
branch_labels = None
depends_on = None


def upgrade():

    op.add_column(
        "opportunities",
        sa.Column(
            "is_deleted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    op.add_column(
        "opportunities",
        sa.Column(
            "deleted_at",
            sa.DateTime(),
            nullable=True,
        ),
    )


def downgrade():

    op.drop_column("opportunities", "deleted_at")
    op.drop_column("opportunities", "is_deleted")