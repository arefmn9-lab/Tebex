from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base


class Worker(Base):
    __tablename__ = "workers"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        index=True,
    )

    name: Mapped[str] = mapped_column(
        String(150),
        nullable=False,
        index=True,
    )

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="idle",
        index=True,
    )

    chrome_profile: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    proxy: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    platform: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
        index=True,
    )

    current_job: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        index=True,
    )

    heartbeat: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )

    last_activity: Mapped[datetime | None] = mapped_column(
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
