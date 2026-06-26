from sqlalchemy.orm import Session

from modules.telegram.models.telegram_account import TelegramAccount


class TelegramAccountRepository:
    @staticmethod
    def create(db: Session, account: TelegramAccount):
        db.add(account)
        db.commit()
        db.refresh(account)
        return account

    @staticmethod
    def get_by_communication_account_id(
        db: Session,
        communication_account_id: int,
    ):
        return (
            db.query(TelegramAccount)
            .filter(
                TelegramAccount.communication_account_id == communication_account_id
            )
            .first()
        )

    @staticmethod
    def save(db: Session, account: TelegramAccount):
        db.commit()
        db.refresh(account)
        return account
