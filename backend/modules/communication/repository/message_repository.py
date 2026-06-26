from modules.communication.models.message import Message
from modules.communication.repository.base_repository import CommunicationBaseRepository


class MessageRepository(CommunicationBaseRepository):
    model = Message
