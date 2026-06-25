from datetime import datetime

from sqlalchemy.orm import Session

from modules.sales.models.opportunity import Opportunity
from modules.sales.schemas.opportunity_create import OpportunityCreate
from modules.sales.schemas.opportunity_search import OpportunitySearch
from modules.sales.schemas.opportunity_update import OpportunityUpdate


class OpportunityRepository:

    @staticmethod
    def create(db: Session, data: OpportunityCreate) -> Opportunity:

        opportunity = Opportunity(
            full_name=data.full_name,
            phone=data.phone,
            source=data.source,
            service=data.service,
        )

        db.add(opportunity)
        db.commit()
        db.refresh(opportunity)

        return opportunity

    @staticmethod
    def get_all(
        db: Session,
        page: int = 1,
        page_size: int = 20,
    ):

        return (
            db.query(Opportunity)
            .filter(Opportunity.is_deleted == False)
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )

    @staticmethod
    def get_by_id(
        db: Session,
        opportunity_id: int,
    ):

        return (
            db.query(Opportunity)
            .filter(
                Opportunity.id == opportunity_id,
                Opportunity.is_deleted == False,
            )
            .first()
        )

    @staticmethod
    def get_by_phone(
        db: Session,
        phone: str,
    ):

        return (
            db.query(Opportunity)
            .filter(
                Opportunity.phone == phone,
                Opportunity.is_deleted == False,
            )
            .first()
        )

    @staticmethod
    def search(
        db: Session,
        data: OpportunitySearch,
    ):

        query = (
            db.query(Opportunity)
            .filter(Opportunity.is_deleted == False)
        )

        if data.full_name:
            query = query.filter(
                Opportunity.full_name.ilike(f"%{data.full_name}%")
            )

        if data.phone:
            query = query.filter(
                Opportunity.phone.ilike(f"%{data.phone}%")
            )

        if data.source:
            query = query.filter(
                Opportunity.source == data.source
            )

        if data.service:
            query = query.filter(
                Opportunity.service == data.service
            )

        if data.status:
            query = query.filter(
                Opportunity.status == data.status
            )

        return query.all()

    @staticmethod
    def update(
        db: Session,
        opportunity: Opportunity,
        data: OpportunityUpdate,
    ) -> Opportunity:

        update_data = data.model_dump(exclude_unset=True)

        for field, value in update_data.items():
            setattr(opportunity, field, value)

        db.commit()
        db.refresh(opportunity)

        return opportunity

    @staticmethod
    def delete(
        db: Session,
        opportunity: Opportunity,
    ):

        opportunity.is_deleted = True
        opportunity.deleted_at = datetime.utcnow()

        db.commit()
        db.refresh(opportunity)

        return opportunity