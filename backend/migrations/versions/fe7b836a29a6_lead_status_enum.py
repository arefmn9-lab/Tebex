"""lead status enum

Revision ID: fe7b836a29a6
Revises: cb1210eead80
Create Date: 2026-06-25
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision = "fe7b836a29a6"
down_revision = "cb1210eead80"
branch_labels = None
depends_on = None


def upgrade():

    op.add_column(
        "opportunities",
        sa.Column(
            "clinic_id",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )

    op.add_column(
        "opportunities",
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )

    leadstatus = sa.Enum(
        "new",
        "contacted",
        "consulting",
        "booked",
        "visited",
        "sold",
        "lost",
        name="leadstatus",
    )

    leadstatus.create(op.get_bind(), checkfirst=True)

    op.execute("""
        ALTER TABLE opportunities
        ALTER COLUMN status
        TYPE leadstatus
        USING status::leadstatus
    """)

    op.create_index(
        "ix_opportunities_clinic_id",
        "opportunities",
        ["clinic_id"],
    )


def downgrade():

    op.drop_index(
        "ix_opportunities_clinic_id",
        table_name="opportunities",
    )

    op.execute("""
        ALTER TABLE opportunities
        ALTER COLUMN status
        TYPE VARCHAR(50)
    """)

    sa.Enum(name="leadstatus").drop(
        op.get_bind(),
        checkfirst=True,
    )

    op.drop_column("opportunities", "updated_at")
    op.drop_column("opportunities", "clinic_id")