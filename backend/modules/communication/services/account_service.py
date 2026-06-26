from modules.communication.repository.account_repository import AccountRepository
from modules.communication.services.base_service import CommunicationBaseService


class AccountService(CommunicationBaseService):
    repository = AccountRepository
