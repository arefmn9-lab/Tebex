from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class DashboardOverview(BaseModel):
    accounts_total: int
    active_accounts: int
    messages_sent_today: int
    system_status: str
    warmup_accounts: int = 0
    queued_messages: int = 0
    success_rate: float = 0.0
    fail_rate: float = 0.0

    model_config = ConfigDict(from_attributes=True)


class SendMessageRequest(BaseModel):
    platform: str
    account_id: int
    message: str = Field(min_length=1)
    target: str = Field(min_length=1)
    history: Optional[list[dict[str, Any]]] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunAIRequest(BaseModel):
    platform: str
    account_id: int
    message: str = Field(min_length=1)
    target: str = Field(min_length=1)
    history: Optional[list[dict[str, Any]]] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
