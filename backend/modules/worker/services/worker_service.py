from modules.worker.repository.worker_repository import WorkerRepository
from modules.worker.services.base_service import WorkerBaseService


class WorkerService(WorkerBaseService):
    repository = WorkerRepository
