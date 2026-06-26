from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class AccountCreate(BaseModel):
    clinic_id: int = 1
    platform_id: int
    external_id: Optional[str] = Field(default=None, max_length=150)
    username: Optional[str] = Field(default=None, max_length=150)
    display_name: Optional[str] = Field(default=None, max_length=150)
    phone: Optional[str] = Field(default=None, max_length=30)
    is_active: bool = True

    model_config = ConfigDict(from_attributes=True)


class AccountUpdate(BaseModel):
    clinic_id: Optional[int] = None
    platform_id: Optional[int] = None
    external_id: Optional[str] = Field(default=None, max_length=150)
    username: Optional[str] = Field(default=None, max_length=150)
    display_name: Optional[str] = Field(default=None, max_length=150)
    phone: Optional[str] = Field(default=None, max_length=30)
    is_active: Optional[bool] = None

    model_config = ConfigDict(from_attributes=True)


class AccountResponse(BaseModel):
    id: int
    clinic_id: int
    platform_id: int
    external_id: Optional[str]
    username: Optional[str]
    display_name: Optional[str]
    phone: Optional[str]
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
