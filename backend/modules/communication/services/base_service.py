from sqlalchemy.orm import Session


class CommunicationBaseService:
    repository = None

    @classmethod
    def create(cls, db: Session, data):
        return cls.repository.create(db, data)

    @classmethod
    def get_all(
        cls,
        db: Session,
        page: int = 1,
        page_size: int = 20,
    ):
        return cls.repository.get_all(db, page, page_size)

    @classmethod
    def get_by_id(cls, db: Session, item_id: int):
        return cls.repository.get_by_id(db, item_id)

    @classmethod
    def update(cls, db: Session, item_id: int, data):
        item = cls.repository.get_by_id(db, item_id)

        if item is None:
            return None

        return cls.repository.update(db, item, data)

    @classmethod
    def delete(cls, db: Session, item_id: int):
        item = cls.repository.get_by_id(db, item_id)

        if item is None:
            return None

        return cls.repository.delete(db, item)
