from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class PlatformCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=100)
    code: str = Field(..., min_length=2, max_length=50)
    is_active: bool = True

    model_config = ConfigDict(from_attributes=True)


class PlatformUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=2, max_length=100)
    code: Optional[str] = Field(default=None, min_length=2, max_length=50)
    is_active: Optional[bool] = None

    model_config = ConfigDict(from_attributes=True)


class PlatformResponse(BaseModel):
    id: int
    name: str
    code: str
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
