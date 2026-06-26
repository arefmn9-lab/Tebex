from modules.communication.models.job import Job
from modules.communication.repository.base_repository import CommunicationBaseRepository


class JobRepository(CommunicationBaseRepository):
    model = Job
