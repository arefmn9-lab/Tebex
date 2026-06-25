from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from core.database.dependencies import get_db

from modules.sales.schemas.opportunity_create import OpportunityCreate
from modules.sales.schemas.opportunity_update import OpportunityUpdate
from modules.sales.schemas.opportunity_response import OpportunityResponse
from modules.sales.schemas.opportunity_search import OpportunitySearch

from modules.sales.services.opportunity_service import OpportunityService

router = APIRouter(
    prefix="/opportunities",
    tags=["Sales"],
)


@router.post(
    "/",
    response_model=OpportunityResponse,
)
def create_opportunity(
    data: OpportunityCreate,
    db: Session = Depends(get_db),
):
    opportunity = OpportunityService.create(db, data)

    if opportunity is None:
        raise HTTPException(
            status_code=409,
            detail="Phone number already exists",
        )

    return opportunity


@router.post(
    "/search",
    response_model=list[OpportunityResponse],
)
def search_opportunities(
    data: OpportunitySearch,
    db: Session = Depends(get_db),
):
    return OpportunityService.search(
        db,
        data,
    )


@router.get(
    "/",
    response_model=list[OpportunityResponse],
)
def get_opportunities(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    return OpportunityService.get_all(
        db,
        page,
        page_size,
    )


@router.get(
    "/{opportunity_id}",
    response_model=OpportunityResponse,
)
def get_opportunity(
    opportunity_id: int,
    db: Session = Depends(get_db),
):

    opportunity = OpportunityService.get_by_id(
        db,
        opportunity_id,
    )

    if opportunity is None:
        raise HTTPException(
            status_code=404,
            detail="Opportunity not found",
        )

    return opportunity


@router.patch(
    "/{opportunity_id}",
    response_model=OpportunityResponse,
)
def update_opportunity(
    opportunity_id: int,
    data: OpportunityUpdate,
    db: Session = Depends(get_db),
):

    opportunity = OpportunityService.update(
        db,
        opportunity_id,
        data,
    )

    if opportunity is None:
        raise HTTPException(
            status_code=404,
            detail="Opportunity not found",
        )

    return opportunity


@router.delete(
    "/{opportunity_id}",
)
def delete_opportunity(
    opportunity_id: int,
    db: Session = Depends(get_db),
):

    opportunity = OpportunityService.delete(
        db,
        opportunity_id,
    )

    if opportunity is None:
        raise HTTPException(
            status_code=404,
            detail="Opportunity not found",
        )

    return {
        "message": "Opportunity deleted successfully",
    }