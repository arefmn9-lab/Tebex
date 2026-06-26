from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class JobCreate(BaseModel):
    clinic_id: int = 1
    platform_id: int
    account_id: Optional[int] = None
    conversation_id: Optional[int] = None
    message_id: Optional[int] = None
    job_type: str = Field(..., max_length=50)
    status: str = Field(default="pending", max_length=50)
    priority: int = 0
    payload: Optional[dict[str, Any]] = None
    error_message: Optional[str] = None
    retry_count: int = 0
    scheduled_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class JobUpdate(BaseModel):
    clinic_id: Optional[int] = None
    platform_id: Optional[int] = None
    account_id: Optional[int] = None
    conversation_id: Optional[int] = None
    message_id: Optional[int] = None
    job_type: Optional[str] = Field(default=None, max_length=50)
    status: Optional[str] = Field(default=None, max_length=50)
    priority: Optional[int] = None
    payload: Optional[dict[str, Any]] = None
    error_message: Optional[str] = None
    retry_count: Optional[int] = None
    scheduled_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class JobResponse(BaseModel):
    id: int
    clinic_id: int
    platform_id: int
    account_id: Optional[int]
    conversation_id: Optional[int]
    message_id: Optional[int]
    job_type: str
    status: str
    priority: int
    payload: Optional[dict[str, Any]]
    error_message: Optional[str]
    retry_count: int
    scheduled_at: Optional[datetime]
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
