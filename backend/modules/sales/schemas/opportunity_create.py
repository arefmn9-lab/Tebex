from pydantic import BaseModel, ConfigDict, Field


class OpportunityCreate(BaseModel):

    full_name: str = Field(
        ...,
        min_length=2,
        max_length=150,
    )

    phone: str = Field(
        ...,
        min_length=8,
        max_length=20,
    )

    source: str

    service: str

    model_config = ConfigDict(
        from_attributes=True
    )