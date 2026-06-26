from sqlalchemy.orm import Session

from modules.telegram.services.telegram_adapter import TelegramAdapter


class TelegramHealthService:
    @staticmethod
    def get_health(db: Session, communication_account_id: int | None = None):
        adapter = TelegramAdapter()
        return adapter.health_check(db, communication_account_id)
