from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from shared.constants.priority_level import PriorityLevel
from shared.constants.queue_status import QueueStatus


class QueueCreate(BaseModel):
    job_id: int
    priority: PriorityLevel = PriorityLevel.NORMAL
    status: QueueStatus = QueueStatus.PENDING
    attempts: int = 0
    max_attempts: int = 3
    scheduled_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    worker_id: Optional[int] = None

    model_config = ConfigDict(from_attributes=True)


class QueueUpdate(BaseModel):
    job_id: Optional[int] = None
    priority: Optional[PriorityLevel] = None
    status: Optional[QueueStatus] = None
    attempts: Optional[int] = Field(default=None, ge=0)
    max_attempts: Optional[int] = Field(default=None, ge=1)
    scheduled_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    worker_id: Optional[int] = None

    model_config = ConfigDict(from_attributes=True)


class QueueResponse(BaseModel):
    id: int
    job_id: int
    priority: str
    status: str
    attempts: int
    max_attempts: int
    scheduled_at: Optional[datetime]
    started_at: Optional[datetime]
    finished_at: Optional[datetime]
    worker_id: Optional[int]
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
