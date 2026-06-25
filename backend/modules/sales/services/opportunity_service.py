from sqlalchemy.orm import Session

from modules.sales.repository.opportunity_repository import OpportunityRepository
from modules.sales.schemas.opportunity_create import OpportunityCreate
from modules.sales.schemas.opportunity_update import OpportunityUpdate
from modules.sales.schemas.opportunity_search import OpportunitySearch


class OpportunityService:

    @staticmethod
    def create(
        db: Session,
        data: OpportunityCreate,
    ):

        existing = OpportunityRepository.get_by_phone(
            db,
            data.phone,
        )

        if existing:
            return None

        return OpportunityRepository.create(
            db,
            data,
        )

    @staticmethod
    def get_all(
        db: Session,
        page: int = 1,
        page_size: int = 20,
    ):

        return OpportunityRepository.get_all(
            db,
            page,
            page_size,
        )

    @staticmethod
    def search(
        db: Session,
        data: OpportunitySearch,
    ):

        return OpportunityRepository.search(
            db,
            data,
        )

    @staticmethod
    def get_by_id(
        db: Session,
        opportunity_id: int,
    ):

        return OpportunityRepository.get_by_id(
            db,
            opportunity_id,
        )

    @staticmethod
    def update(
        db: Session,
        opportunity_id: int,
        data: OpportunityUpdate,
    ):

        opportunity = OpportunityRepository.get_by_id(
            db,
            opportunity_id,
        )

        if opportunity is None:
            return None

        return OpportunityRepository.update(
            db,
            opportunity,
            data,
        )