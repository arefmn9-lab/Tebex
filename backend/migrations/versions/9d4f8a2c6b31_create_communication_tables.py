"""create communication tables

Revision ID: 9d4f8a2c6b31
Revises: 4c8aeb6f4492
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "9d4f8a2c6b31"
down_revision: Union[str, Sequence[str], None] = "4c8aeb6f4492"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "communication_platforms",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("code", sa.String(length=50), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_communication_platforms_id", "communication_platforms", ["id"])
    op.create_index("ix_communication_platforms_code", "communication_platforms", ["code"], unique=True)

    op.create_table(
        "communication_accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("clinic_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("platform_id", sa.Integer(), nullable=False),
        sa.Column("external_id", sa.String(length=150), nullable=True),
        sa.Column("username", sa.String(length=150), nullable=True),
        sa.Column("display_name", sa.String(length=150), nullable=True),
        sa.Column("phone", sa.String(length=30), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["platform_id"], ["communication_platforms.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_communication_accounts_id", "communication_accounts", ["id"])
    op.create_index("ix_communication_accounts_clinic_id", "communication_accounts", ["clinic_id"])
    op.create_index("ix_communication_accounts_platform_id", "communication_accounts", ["platform_id"])
    op.create_index("ix_communication_accounts_external_id", "communication_accounts", ["external_id"])
    op.create_index("ix_communication_accounts_username", "communication_accounts", ["username"])
    op.create_index("ix_communication_accounts_phone", "communication_accounts", ["phone"])

    op.create_table(
        "communication_conversations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("clinic_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("platform_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("external_conversation_id", sa.String(length=150), nullable=True),
        sa.Column("contact_name", sa.String(length=150), nullable=True),
        sa.Column("contact_identifier", sa.String(length=150), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="open"),
        sa.Column("last_message_at", sa.DateTime(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["account_id"], ["communication_accounts.id"]),
        sa.ForeignKeyConstraint(["platform_id"], ["communication_platforms.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_communication_conversations_id", "communication_conversations", ["id"])
    op.create_index("ix_communication_conversations_clinic_id", "communication_conversations", ["clinic_id"])
    op.create_index("ix_communication_conversations_platform_id", "communication_conversations", ["platform_id"])
    op.create_index("ix_communication_conversations_account_id", "communication_conversations", ["account_id"])
    op.create_index("ix_communication_conversations_external_conversation_id", "communication_conversations", ["external_conversation_id"])
    op.create_index("ix_communication_conversations_contact_identifier", "communication_conversations", ["contact_identifier"])
    op.create_index("ix_communication_conversations_status", "communication_conversations", ["status"])

    op.create_table(
        "communication_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("clinic_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("platform_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("external_message_id", sa.String(length=150), nullable=True),
        sa.Column("direction", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="pending"),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
        sa.Column("read_at", sa.DateTime(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["account_id"], ["communication_accounts.id"]),
        sa.ForeignKeyConstraint(["conversation_id"], ["communication_conversations.id"]),
        sa.ForeignKeyConstraint(["platform_id"], ["communication_platforms.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_communication_messages_id", "communication_messages", ["id"])
    op.create_index("ix_communication_messages_clinic_id", "communication_messages", ["clinic_id"])
    op.create_index("ix_communication_messages_platform_id", "communication_messages", ["platform_id"])
    op.create_index("ix_communication_messages_account_id", "communication_messages", ["account_id"])
    op.create_index("ix_communication_messages_conversation_id", "communication_messages", ["conversation_id"])
    op.create_index("ix_communication_messages_external_message_id", "communication_messages", ["external_message_id"])
    op.create_index("ix_communication_messages_direction", "communication_messages", ["direction"])
    op.create_index("ix_communication_messages_status", "communication_messages", ["status"])

    op.create_table(
        "communication_jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("clinic_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("platform_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("conversation_id", sa.Integer(), nullable=True),
        sa.Column("message_id", sa.Integer(), nullable=True),
        sa.Column("job_type", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="pending"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("scheduled_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["account_id"], ["communication_accounts.id"]),
        sa.ForeignKeyConstraint(["conversation_id"], ["communication_conversations.id"]),
        sa.ForeignKeyConstraint(["message_id"], ["communication_messages.id"]),
        sa.ForeignKeyConstraint(["platform_id"], ["communication_platforms.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_communication_jobs_id", "communication_jobs", ["id"])
    op.create_index("ix_communication_jobs_clinic_id", "communication_jobs", ["clinic_id"])
    op.create_index("ix_communication_jobs_platform_id", "communication_jobs", ["platform_id"])
    op.create_index("ix_communication_jobs_account_id", "communication_jobs", ["account_id"])
    op.create_index("ix_communication_jobs_conversation_id", "communication_jobs", ["conversation_id"])
    op.create_index("ix_communication_jobs_message_id", "communication_jobs", ["message_id"])
    op.create_index("ix_communication_jobs_job_type", "communication_jobs", ["job_type"])
    op.create_index("ix_communication_jobs_status", "communication_jobs", ["status"])


def downgrade():
    op.drop_index("ix_communication_jobs_status", table_name="communication_jobs")
    op.drop_index("ix_communication_jobs_job_type", table_name="communication_jobs")
    op.drop_index("ix_communication_jobs_message_id", table_name="communication_jobs")
    op.drop_index("ix_communication_jobs_conversation_id", table_name="communication_jobs")
    op.drop_index("ix_communication_jobs_account_id", table_name="communication_jobs")
    op.drop_index("ix_communication_jobs_platform_id", table_name="communication_jobs")
    op.drop_index("ix_communication_jobs_clinic_id", table_name="communication_jobs")
    op.drop_index("ix_communication_jobs_id", table_name="communication_jobs")
    op.drop_table("communication_jobs")

    op.drop_index("ix_communication_messages_status", table_name="communication_messages")
    op.drop_index("ix_communication_messages_direction", table_name="communication_messages")
    op.drop_index("ix_communication_messages_external_message_id", table_name="communication_messages")
    op.drop_index("ix_communication_messages_conversation_id", table_name="communication_messages")
    op.drop_index("ix_communication_messages_account_id", table_name="communication_messages")
    op.drop_index("ix_communication_messages_platform_id", table_name="communication_messages")
    op.drop_index("ix_communication_messages_clinic_id", table_name="communication_messages")
    op.drop_index("ix_communication_messages_id", table_name="communication_messages")
    op.drop_table("communication_messages")

    op.drop_index("ix_communication_conversations_status", table_name="communication_conversations")
    op.drop_index("ix_communication_conversations_contact_identifier", table_name="communication_conversations")
    op.drop_index("ix_communication_conversations_external_conversation_id", table_name="communication_conversations")
    op.drop_index("ix_communication_conversations_account_id", table_name="communication_conversations")
    op.drop_index("ix_communication_conversations_platform_id", table_name="communication_conversations")
    op.drop_index("ix_communication_conversations_clinic_id", table_name="communication_conversations")
    op.drop_index("ix_communication_conversations_id", table_name="communication_conversations")
    op.drop_table("communication_conversations")

    op.drop_index("ix_communication_accounts_phone", table_name="communication_accounts")
    op.drop_index("ix_communication_accounts_username", table_name="communication_accounts")
    op.drop_index("ix_communication_accounts_external_id", table_name="communication_accounts")
    op.drop_index("ix_communication_accounts_platform_id", table_name="communication_accounts")
    op.drop_index("ix_communication_accounts_clinic_id", table_name="communication_accounts")
    op.drop_index("ix_communication_accounts_id", table_name="communication_accounts")
    op.drop_table("communication_accounts")

    op.drop_index("ix_communication_platforms_code", table_name="communication_platforms")
    op.drop_index("ix_communication_platforms_id", table_name="communication_platforms")
    op.drop_table("communication_platforms")
