from modules.communication.repository.message_repository import MessageRepository
from modules.communication.services.base_service import CommunicationBaseService


class MessageService(CommunicationBaseService):
    repository = MessageRepository
