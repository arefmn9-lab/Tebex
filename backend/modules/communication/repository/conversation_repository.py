from modules.communication.models.conversation import Conversation
from modules.communication.repository.base_repository import CommunicationBaseRepository


class ConversationRepository(CommunicationBaseRepository):
    model = Conversation
