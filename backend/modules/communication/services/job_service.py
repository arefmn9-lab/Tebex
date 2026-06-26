from modules.communication.repository.job_repository import JobRepository
from modules.communication.services.base_service import CommunicationBaseService


class JobService(CommunicationBaseService):
    repository = JobRepository
