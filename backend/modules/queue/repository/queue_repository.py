from datetime import datetime

from sqlalchemy import case

from modules.queue.models.queue_item import QueueItem
from modules.queue.repository.base_repository import QueueBaseRepository


class QueueRepository(QueueBaseRepository):
    model = QueueItem

    @staticmethod
    def get_by_job_id(db, job_id: int):
        return (
            db.query(QueueItem)
            .filter(QueueItem.job_id == job_id)
            .first()
        )

    @staticmethod
    def get_ready_items(db):
        return (
            db.query(QueueItem)
            .filter(
                QueueItem.status.in_(["pending", "retry"]),
                (QueueItem.scheduled_at.is_(None)) | (QueueItem.scheduled_at <= datetime.utcnow()),
            )
            .order_by(
                case(
                    (QueueItem.priority == "high", 0),
                    (QueueItem.priority == "normal", 1),
                    else_=2,
                ),
                QueueItem.scheduled_at.asc().nullsfirst(),
                QueueItem.created_at.asc(),
            )
            .all()
        )
