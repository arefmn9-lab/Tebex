from sqlalchemy.orm import Session

from modules.queue.repository.queue_repository import QueueRepository
from modules.queue.services.base_service import QueueBaseService


class QueueService(QueueBaseService):
    repository = QueueRepository

    @classmethod
    def create(cls, db: Session, data):
        existing = QueueRepository.get_by_job_id(db, data.job_id)

        if existing:
            return None

        return QueueRepository.create(db, data)

    @classmethod
    def get_by_job_id(cls, db: Session, job_id: int):
        return QueueRepository.get_by_job_id(db, job_id)
