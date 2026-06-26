"""create queue and worker tables

Revision ID: 8c4c6f75e9ad
Revises: 9d4f8a2c6b31
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "8c4c6f75e9ad"
down_revision: Union[str, Sequence[str], None] = "9d4f8a2c6b31"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "workers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="idle"),
        sa.Column("chrome_profile", sa.String(length=255), nullable=True),
        sa.Column("proxy", sa.String(length=255), nullable=True),
        sa.Column("platform", sa.String(length=50), nullable=True),
        sa.Column("current_job", sa.Integer(), nullable=True),
        sa.Column("heartbeat", sa.DateTime(), nullable=True),
        sa.Column("last_activity", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_workers_id", "workers", ["id"])
    op.create_index("ix_workers_name", "workers", ["name"])
    op.create_index("ix_workers_status", "workers", ["status"])
    op.create_index("ix_workers_platform", "workers", ["platform"])
    op.create_index("ix_workers_current_job", "workers", ["current_job"])

    op.create_table(
        "queue_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("priority", sa.String(length=20), nullable=False, server_default="normal"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("scheduled_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("worker_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["job_id"], ["communication_jobs.id"]),
        sa.ForeignKeyConstraint(["worker_id"], ["workers.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id"),
    )
    op.create_index("ix_queue_items_id", "queue_items", ["id"])
    op.create_index("ix_queue_items_job_id", "queue_items", ["job_id"], unique=True)
    op.create_index("ix_queue_items_priority", "queue_items", ["priority"])
    op.create_index("ix_queue_items_status", "queue_items", ["status"])
    op.create_index("ix_queue_items_scheduled_at", "queue_items", ["scheduled_at"])
    op.create_index("ix_queue_items_worker_id", "queue_items", ["worker_id"])


def downgrade():
    op.drop_index("ix_queue_items_worker_id", table_name="queue_items")
    op.drop_index("ix_queue_items_scheduled_at", table_name="queue_items")
    op.drop_index("ix_queue_items_status", table_name="queue_items")
    op.drop_index("ix_queue_items_priority", table_name="queue_items")
    op.drop_index("ix_queue_items_job_id", table_name="queue_items")
    op.drop_index("ix_queue_items_id", table_name="queue_items")
    op.drop_table("queue_items")

    op.drop_index("ix_workers_current_job", table_name="workers")
    op.drop_index("ix_workers_platform", table_name="workers")
    op.drop_index("ix_workers_status", table_name="workers")
    op.drop_index("ix_workers_name", table_name="workers")
    op.drop_index("ix_workers_id", table_name="workers")
    op.drop_table("workers")
