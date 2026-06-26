from datetime import datetime

from sqlalchemy.orm import Session

from modules.telegram.exceptions import InvalidSession
from modules.telegram.models.telegram_account import TelegramAccount
from modules.telegram.repository.telegram_account_repository import (
    TelegramAccountRepository,
)


class SessionManager:
    @staticmethod
    def create_session(
        db: Session,
        communication_account_id: int,
        phone_number: str | None = None,
        session_name: str | None = None,
    ):
        account = TelegramAccountRepository.get_by_communication_account_id(
            db,
            communication_account_id,
        )

        if account is None:
            account = TelegramAccount(
                communication_account_id=communication_account_id,
                phone_number=phone_number,
                session_name=session_name or f"telegram_{communication_account_id}",
                status="disconnected",
            )
            return TelegramAccountRepository.create(db, account)

        account.phone_number = phone_number or account.phone_number
        account.session_name = session_name or account.session_name
        account.updated_at = datetime.utcnow()
        return TelegramAccountRepository.save(db, account)

    @staticmethod
    def load_session(db: Session, communication_account_id: int):
        return TelegramAccountRepository.get_by_communication_account_id(
            db,
            communication_account_id,
        )

    @staticmethod
    def close_session(db: Session, account: TelegramAccount):
        account.status = "disconnected"
        account.updated_at = datetime.utcnow()
        return TelegramAccountRepository.save(db, account)

    @staticmethod
    def reconnect_session(db: Session, account: TelegramAccount):
        if not SessionManager.validate_session(account):
            raise InvalidSession("Telegram session is invalid")

        account.status = "connected"
        account.last_login = datetime.utcnow()
        account.updated_at = datetime.utcnow()
        return TelegramAccountRepository.save(db, account)

    @staticmethod
    def validate_session(account: TelegramAccount | None):
        if account is None:
            return False

        return bool(account.session_name)
