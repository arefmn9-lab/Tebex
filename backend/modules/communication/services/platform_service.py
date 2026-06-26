from sqlalchemy.orm import Session

from modules.communication.repository.platform_repository import PlatformRepository
from modules.communication.schemas.platform import PlatformCreate
from modules.communication.services.base_service import CommunicationBaseService


class PlatformService(CommunicationBaseService):
    repository = PlatformRepository

    @classmethod
    def create(cls, db: Session, data: PlatformCreate):
        existing = PlatformRepository.get_by_code(db, data.code)

        if existing:
            return None

        return PlatformRepository.create(db, data)
