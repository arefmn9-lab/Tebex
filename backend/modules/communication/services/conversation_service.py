from modules.communication.repository.conversation_repository import ConversationRepository
from modules.communication.services.base_service import CommunicationBaseService


class ConversationService(CommunicationBaseService):
    repository = ConversationRepository
