from sqlalchemy.orm import Session


class WorkerBaseRepository:
    model = None

    @classmethod
    def create(cls, db: Session, data):
        item = cls.model(**data.model_dump(mode="json"))
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
        return (
            db.query(cls.model)
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )

    @classmethod
    def get_by_id(cls, db: Session, item_id: int):
        return db.query(cls.model).filter(cls.model.id == item_id).first()

    @classmethod
    def update(cls, db: Session, item, data):
        update_data = data.model_dump(exclude_unset=True, mode="json")

        for field, value in update_data.items():
            setattr(item, field, value)

        db.commit()
        db.refresh(item)
        return item
