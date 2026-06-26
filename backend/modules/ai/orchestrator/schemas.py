from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class ExecutionPlan(BaseModel):
    platform_name: str
    priority_score: int = Field(ge=0, le=100)
    adapter_available: bool
    reason: str
    metadata: Optional[dict[str, Any]] = None

    model_config = ConfigDict(from_attributes=True)
