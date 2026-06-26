from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class TelegramConnectRequest(BaseModel):
    communication_account_id: int
    phone_number: Optional[str] = Field(default=None, max_length=30)
    session_name: Optional[str] = Field(default=None, max_length=150)

    model_config = ConfigDict(from_attributes=True)


class TelegramDisconnectRequest(BaseModel):
    communication_account_id: int

    model_config = ConfigDict(from_attributes=True)


class TelegramSendRequest(BaseModel):
    communication_account_id: int
    target: str = Field(min_length=1, max_length=150)
    text: str = Field(min_length=1, max_length=4000)

    model_config = ConfigDict(from_attributes=True)


class TelegramAccountResponse(BaseModel):
    id: int
    communication_account_id: int
    phone_number: Optional[str]
    api_id: Optional[str]
    session_name: Optional[str]
    session_path: Optional[str]
    status: str
    last_login: Optional[datetime]
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TelegramStatusResponse(BaseModel):
    connected: bool
    status: str
    session_valid: bool
    account: Optional[TelegramAccountResponse] = None

    model_config = ConfigDict(from_attributes=True)


class TelegramSendResponse(BaseModel):
    success: bool
    status: str
    target: str
    message: str

    model_config = ConfigDict(from_attributes=True)


class TelegramHealthResponse(BaseModel):
    connected: bool
    connection_status: str
    session_status: str

    model_config = ConfigDict(from_attributes=True)
