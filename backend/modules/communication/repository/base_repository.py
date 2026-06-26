from datetime import datetime

from sqlalchemy.orm import Session


class CommunicationBaseRepository:
    model = None

    @classmethod
    def create(cls, db: Session, data):
        item = cls.model(**data.model_dump())

        db.add(item)
        db.commit()
        db.refresh(item)

        return item

    @classmethod
    def get_all(
        cls,
        db: Session,
        page: int = 1,
        page_size: int = 20,
    ):
        query = db.query(cls.model)

        if hasattr(cls.model, "is_deleted"):
            query = query.filter(cls.model.is_deleted == False)

        return (
            query
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )

    @classmethod
    def get_by_id(cls, db: Session, item_id: int):
        query = db.query(cls.model).filter(cls.model.id == item_id)

        if hasattr(cls.model, "is_deleted"):
            query = query.filter(cls.model.is_deleted == False)

        return query.first()

    @classmethod
    def update(cls, db: Session, item, data):
        update_data = data.model_dump(exclude_unset=True)

        for field, value in update_data.items():
            setattr(item, field, value)

        db.commit()
        db.refresh(item)

        return item

    @classmethod
    def delete(cls, db: Session, item):
        if hasattr(item, "is_deleted"):
            item.is_deleted = True
            item.deleted_at = datetime.utcnow()
        else:
            db.delete(item)

        db.commit()
        db.refresh(item)

        return item
