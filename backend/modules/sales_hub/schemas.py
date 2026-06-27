from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class SalesIntent(BaseModel):
    type: str
    confidence: float = Field(ge=0.0, le=1.0)

    model_config = ConfigDict(from_attributes=True)


class SalesStrategy(BaseModel):
    name: str
    prompt_template: str
    goal: str
    tone: str

    model_config = ConfigDict(from_attributes=True)


class SalesContext(BaseModel):
    user_message: str = Field(min_length=1)
    platform: str
    history: Optional[list[dict[str, Any]]] = None
    account_id: Optional[int] = None
    chat_id: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(from_attributes=True)
