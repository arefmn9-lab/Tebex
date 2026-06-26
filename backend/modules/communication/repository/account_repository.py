from modules.communication.models.account import Account
from modules.communication.repository.base_repository import CommunicationBaseRepository


class AccountRepository(CommunicationBaseRepository):
    model = Account
