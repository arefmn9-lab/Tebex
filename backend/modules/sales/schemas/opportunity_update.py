from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from shared.constants.lead_status import LeadStatus


class OpportunityUpdate(BaseModel):

    full_name: Optional[str] = Field(
        default=None,
        min_length=2,
        max_length=150,
    )

    phone: Optional[str] = Field(
        default=None,
        min_length=8,
        max_length=20,
    )

    source: Optional[str] = None

    service: Optional[str] = None

    status: Optional[LeadStatus] = None

    model_config = ConfigDict(
        from_attributes=True
    )