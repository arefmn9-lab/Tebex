from sqlalchemy.orm import Session

from modules.communication.models.platform import Platform
from modules.communication.repository.base_repository import CommunicationBaseRepository


class PlatformRepository(CommunicationBaseRepository):
    model = Platform

    @staticmethod
    def get_by_code(db: Session, code: str):
        return (
            db.query(Platform)
            .filter(
                Platform.code == code,
                Platform.is_deleted == False,
            )
            .first()
        )
