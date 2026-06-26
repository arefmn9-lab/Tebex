from modules.worker.models.worker import Worker
from modules.worker.repository.base_repository import WorkerBaseRepository
from shared.constants.worker_status import WorkerStatus


class WorkerRepository(WorkerBaseRepository):
    model = Worker

    @staticmethod
    def get_idle_worker(db, platform: str | None = None):
        query = db.query(Worker).filter(Worker.status == WorkerStatus.IDLE.value)

        if platform:
            query = query.filter(Worker.platform == platform)

        return (
            query
            .order_by(Worker.last_activity.asc().nullsfirst(), Worker.created_at.asc())
            .first()
        )

    @staticmethod
    def get_idle_workers(db, platform: str | None = None):
        query = db.query(Worker).filter(Worker.status == WorkerStatus.IDLE.value)

        if platform:
            query = query.filter(Worker.platform == platform)

        return (
            query
            .order_by(Worker.last_activity.asc().nullsfirst(), Worker.created_at.asc())
            .all()
        )
