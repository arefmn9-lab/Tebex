from typing import Optional

from pydantic import BaseModel

from shared.constants.lead_status import LeadStatus


class OpportunitySearch(BaseModel):

    full_name: Optional[str] = None

    phone: Optional[str] = None

    source: Optional[str] = None

    service: Optional[str] = None

    status: Optional[LeadStatus] = None