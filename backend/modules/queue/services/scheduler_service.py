from datetime import datetime

from sqlalchemy.orm import Session

from modules.communication.models.job import Job
from modules.queue.models.queue_item import QueueItem
from modules.queue.repository.queue_repository import QueueRepository
from modules.worker.models.worker import Worker
from modules.worker.repository.worker_repository import WorkerRepository
from shared.constants.queue_status import QueueStatus
from shared.constants.worker_status import WorkerStatus


class SchedulerService:
    @staticmethod
    def enqueue_job(
        db: Session,
        job_id: int,
        priority: str = "normal",
        scheduled_at: datetime | None = None,
        max_attempts: int = 3,
    ):
        existing = QueueRepository.get_by_job_id(db, job_id)
        if existing:
            return existing

        job = db.query(Job).filter(Job.id == job_id).first()
        if job is None:
            return None

        queue_item = QueueItem(
            job_id=job_id,
            priority=priority,
            status=QueueStatus.PENDING.value,
            attempts=0,
            max_attempts=max_attempts,
            scheduled_at=scheduled_at,
        )
        db.add(queue_item)
        db.commit()
        db.refresh(queue_item)
        return queue_item

    @staticmethod
    def select_idle_worker(db: Session, platform: str | None = None):
        return WorkerRepository.get_idle_worker(db, platform)

    @staticmethod
    def assign_jobs(db: Session, limit: int | None = None):
        ready_items = QueueRepository.get_ready_items(db)
        if not ready_items:
            return None

        workers = WorkerRepository.get_idle_workers(db)
        if not workers:
            return []

        now = datetime.utcnow()
        assigned = []
        assignment_count = 0

        for queue_item, worker in zip(ready_items, workers, strict=False):
            if limit is not None and assignment_count >= limit:
                break

            queue_item.status = QueueStatus.RUNNING.value
            queue_item.started_at = now
            queue_item.worker_id = worker.id
            queue_item.updated_at = now

            worker.status = WorkerStatus.BUSY.value
            worker.current_job = queue_item.id
            worker.heartbeat = now
            worker.last_activity = now
            worker.updated_at = now

            assigned.append(queue_item)
            assignment_count += 1

        if assigned:
            db.commit()
            for queue_item in assigned:
                db.refresh(queue_item)

        return assigned

    @staticmethod
    def dispatch_next_job(db: Session):
        assigned = SchedulerService.assign_jobs(db, limit=1)
        if not assigned:
            return None
        return assigned[0]

    @staticmethod
    def retry_failed_jobs(db: Session):
        failed_items = (
            db.query(QueueItem)
            .filter(QueueItem.status == QueueStatus.FAILED.value)
            .all()
        )

        retried = []
        now = datetime.utcnow()

        for item in failed_items:
            if item.attempts >= item.max_attempts:
                continue

            item.attempts += 1
            item.status = QueueStatus.RETRY.value
            item.scheduled_at = now
            item.updated_at = now
            retried.append(item)

        if retried:
            db.commit()
            for item in retried:
                db.refresh(item)

        return retried
