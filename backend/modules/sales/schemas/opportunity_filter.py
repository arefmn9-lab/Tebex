from pydantic import BaseModel


class OpportunityFilter(BaseModel):

    status: str | None = None

    source: str | None = None

    service: str | None = None