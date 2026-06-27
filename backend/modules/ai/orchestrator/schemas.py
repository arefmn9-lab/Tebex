from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class ExecutionPlan(BaseModel):
    type: str
    platform: Optional[str] = None
    account_id: Optional[int] = None
    payload: dict[str, Any] = Field(default_factory=dict)
    db: Any = None
    platform_name: Optional[str] = None
    priority_score: int = Field(ge=0, le=100)
    adapter_available: bool = False
    reason: str = ""
    metadata: Optional[dict[str, Any]] = None

    model_config = ConfigDict(from_attributes=True, arbitrary_types_allowed=True)
