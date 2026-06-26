from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class MessageCreate(BaseModel):
    clinic_id: int = 1
    platform_id: int
    account_id: int
    conversation_id: int
    external_message_id: Optional[str] = Field(default=None, max_length=150)
    direction: str = Field(..., max_length=20)
    content: str = Field(..., min_length=1)
    status: str = Field(default="pending", max_length=50)
    sent_at: Optional[datetime] = None
    delivered_at: Optional[datetime] = None
    read_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class MessageUpdate(BaseModel):
    clinic_id: Optional[int] = None
    platform_id: Optional[int] = None
    account_id: Optional[int] = None
    conversation_id: Optional[int] = None
    external_message_id: Optional[str] = Field(default=None, max_length=150)
    direction: Optional[str] = Field(default=None, max_length=20)
    content: Optional[str] = Field(default=None, min_length=1)
    status: Optional[str] = Field(default=None, max_length=50)
    sent_at: Optional[datetime] = None
    delivered_at: Optional[datetime] = None
    read_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class MessageResponse(BaseModel):
    id: int
    clinic_id: int
    platform_id: int
    account_id: int
    conversation_id: int
    external_message_id: Optional[str]
    direction: str
    content: str
    status: str
    sent_at: Optional[datetime]
    delivered_at: Optional[datetime]
    read_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
