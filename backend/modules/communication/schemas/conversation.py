from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class ConversationCreate(BaseModel):
    clinic_id: int = 1
    platform_id: int
    account_id: int
    external_conversation_id: Optional[str] = Field(default=None, max_length=150)
    contact_name: Optional[str] = Field(default=None, max_length=150)
    contact_identifier: Optional[str] = Field(default=None, max_length=150)
    status: str = Field(default="open", max_length=50)
    last_message_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class ConversationUpdate(BaseModel):
    clinic_id: Optional[int] = None
    platform_id: Optional[int] = None
    account_id: Optional[int] = None
    external_conversation_id: Optional[str] = Field(default=None, max_length=150)
    contact_name: Optional[str] = Field(default=None, max_length=150)
    contact_identifier: Optional[str] = Field(default=None, max_length=150)
    status: Optional[str] = Field(default=None, max_length=50)
    last_message_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class ConversationResponse(BaseModel):
    id: int
    clinic_id: int
    platform_id: int
    account_id: int
    external_conversation_id: Optional[str]
    contact_name: Optional[str]
    contact_identifier: Optional[str]
    status: str
    last_message_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
