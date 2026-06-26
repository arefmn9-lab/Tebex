from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from shared.constants.worker_status import WorkerStatus


class WorkerCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=150)
    status: WorkerStatus = WorkerStatus.IDLE
    chrome_profile: Optional[str] = Field(default=None, max_length=255)
    proxy: Optional[str] = Field(default=None, max_length=255)
    platform: Optional[str] = Field(default=None, max_length=50)
    current_job: Optional[int] = None
    heartbeat: Optional[datetime] = None
    last_activity: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class WorkerUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=2, max_length=150)
    status: Optional[WorkerStatus] = None
    chrome_profile: Optional[str] = Field(default=None, max_length=255)
    proxy: Optional[str] = Field(default=None, max_length=255)
    platform: Optional[str] = Field(default=None, max_length=50)
    current_job: Optional[int] = None
    heartbeat: Optional[datetime] = None
    last_activity: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class WorkerResponse(BaseModel):
    id: int
    name: str
    status: str
    chrome_profile: Optional[str]
    proxy: Optional[str]
    platform: Optional[str]
    current_job: Optional[int]
    heartbeat: Optional[datetime]
    last_activity: Optional[datetime]
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
