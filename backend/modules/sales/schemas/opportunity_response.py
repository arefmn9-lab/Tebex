from datetime import datetime

from pydantic import BaseModel, ConfigDict

from shared.constants.lead_status import LeadStatus


class OpportunityResponse(BaseModel):

    id: int

    clinic_id: int

    full_name: str

    phone: str

    source: str

    service: str

    status: LeadStatus

    created_at: datetime

    updated_at: datetime

    model_config = ConfigDict(
        from_attributes=True
    )