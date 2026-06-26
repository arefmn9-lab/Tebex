from abc import ABC, abstractmethod

from sqlalchemy.orm import Session

from modules.platform.schemas.message import UnifiedMessageSchema


class BaseAdapter(ABC):
    @abstractmethod
    def connect(self, db: Session, communication_account_id: int, **kwargs):
        pass

    @abstractmethod
    def disconnect(self, db: Session, communication_account_id: int):
        pass

    @abstractmethod
    def send_message(
        self,
        db: Session,
        communication_account_id: int,
        message: UnifiedMessageSchema | None = None,
        **kwargs,
    ):
        pass

    @abstractmethod
    def get_status(self, db: Session, communication_account_id: int | None = None):
        pass

    @abstractmethod
    def health_check(self, db: Session, communication_account_id: int | None = None):
        pass
