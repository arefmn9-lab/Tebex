from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base


class TelegramAccount(Base):
    __tablename__ = "telegram_accounts"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        index=True,
    )

    communication_account_id: Mapped[int] = mapped_column(
        ForeignKey("communication_accounts.id"),
        nullable=False,
        unique=True,
        index=True,
    )

    phone_number: Mapped[str | None] = mapped_column(
        String(30),
        nullable=True,
        index=True,
    )

    api_id: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    api_hash: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    session_name: Mapped[str | None] = mapped_column(
        String(150),
        nullable=True,
        index=True,
    )

    session_path: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    status: Mapped[str] = mapped_column(
        String(50),
        default="disconnected",
        nullable=False,
        index=True,
    )

    last_login: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )
