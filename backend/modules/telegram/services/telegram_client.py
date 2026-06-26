from modules.telegram.exceptions import ConnectionLost, InvalidSession, UserNotFound
from modules.telegram.models.telegram_account import TelegramAccount
from modules.telegram.services.session_manager import SessionManager


class TelegramClient:
    def connect(self, account: TelegramAccount):
        if not SessionManager.validate_session(account):
            raise InvalidSession("Telegram session is invalid")

        return {
            "connected": True,
            "status": "connected",
        }

    def disconnect(self, account: TelegramAccount):
        return {
            "connected": False,
            "status": "disconnected",
        }

    def send_message(self, account: TelegramAccount, target: str, text: str):
        if account.status != "connected":
            raise ConnectionLost("Telegram account is not connected")

        if not self.resolve_user(target):
            raise UserNotFound("Telegram target was not found")

        return {
            "success": True,
            "status": "sent",
            "target": target,
            "message": text,
        }

    def receive_updates(self, account: TelegramAccount):
        if account.status != "connected":
            raise ConnectionLost("Telegram account is not connected")

        return []

    def resolve_user(self, target: str):
        return bool(target and target.strip())

    def health_check(self, account: TelegramAccount | None):
        session_valid = SessionManager.validate_session(account)
        connected = bool(account and account.status == "connected")

        return {
            "connected": connected,
            "connection_status": "connected" if connected else "disconnected",
            "session_status": "session_valid" if session_valid else "session_invalid",
        }
