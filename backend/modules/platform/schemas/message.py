from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class UnifiedMessageSchema(BaseModel):
    chat_id: str = Field(min_length=1, max_length=150)
    message: str = Field(min_length=1, max_length=4000)
    attachments: Optional[list[dict[str, Any]]] = None
    metadata: Optional[dict[str, Any]] = None

    model_config = ConfigDict(from_attributes=True)
