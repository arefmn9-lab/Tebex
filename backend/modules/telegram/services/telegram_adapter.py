import logging

from sqlalchemy.orm import Session

from modules.platform.schemas.message import UnifiedMessageSchema
from modules.platform.services.base_adapter import BaseAdapter
from modules.telegram.exceptions import InvalidSession
from modules.telegram.repository.telegram_account_repository import (
    TelegramAccountRepository,
)
from modules.telegram.services.session_manager import SessionManager
from modules.telegram.services.telegram_client import TelegramClient

logger = logging.getLogger(__name__)


class TelegramAdapter(BaseAdapter):
    def __init__(self, client: TelegramClient | None = None):
        self.client = client or TelegramClient()

    def connect(
        self,
        db: Session,
        communication_account_id: int,
        phone_number: str | None = None,
        session_name: str | None = None,
    ):
        account = SessionManager.create_session(
            db=db,
            communication_account_id=communication_account_id,
            phone_number=phone_number,
            session_name=session_name,
        )
        try:
            self.client.connect(account)
            connected_account = SessionManager.reconnect_session(db, account)
            logger.info("Telegram mock login completed for account %s", account.id)
            return connected_account
        except Exception:
            logger.exception("Telegram mock login failed")
            raise

    def disconnect(self, db: Session, communication_account_id: int):
        account = self._get_account(db, communication_account_id)
        try:
            self.client.disconnect(account)
            disconnected_account = SessionManager.close_session(db, account)
            logger.info("Telegram mock logout completed for account %s", account.id)
            return disconnected_account
        except Exception:
            logger.exception("Telegram mock logout failed")
            raise

    def send_message(
        self,
        db: Session,
        communication_account_id: int,
        message: UnifiedMessageSchema | None = None,
        target: str | None = None,
        text: str | None = None,
    ):
        if message is None:
            message = UnifiedMessageSchema(
                chat_id=target or "",
                message=text or "",
            )

        account = self._get_account(db, communication_account_id)
        try:
            result = self.client.send_message(
                account,
                message.chat_id,
                message.message,
            )
            logger.info("Telegram mock send completed for account %s", account.id)
            return result
        except Exception:
            logger.exception("Telegram mock send failed")
            raise

    def receive_message(self, db: Session, communication_account_id: int):
        account = self._get_account(db, communication_account_id)
        try:
            updates = self.client.receive_updates(account)
            logger.info("Telegram mock receive completed for account %s", account.id)
            return updates
        except Exception:
            logger.exception("Telegram mock receive failed")
            raise

    def health_check(self, db: Session, communication_account_id: int | None = None):
        account = None
        if communication_account_id is not None:
            account = TelegramAccountRepository.get_by_communication_account_id(
                db,
                communication_account_id,
            )

        return self.client.health_check(account)

    def get_status(self, db: Session, communication_account_id: int | None = None):
        account = None
        if communication_account_id is not None:
            account = TelegramAccountRepository.get_by_communication_account_id(
                db,
                communication_account_id,
            )

        session_valid = SessionManager.validate_session(account)
        return {
            "connected": bool(account and account.status == "connected"),
            "status": account.status if account else "disconnected",
            "session_valid": session_valid,
            "account": account,
        }

    def status(self, db: Session, communication_account_id: int | None = None):
        return self.get_status(db, communication_account_id)

    def _get_account(self, db: Session, communication_account_id: int):
        account = TelegramAccountRepository.get_by_communication_account_id(
            db,
            communication_account_id,
        )

        if account is None or not SessionManager.validate_session(account):
            raise InvalidSession("Telegram session is invalid")

        return account
