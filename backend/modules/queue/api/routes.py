from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from core.database.dependencies import get_db
from modules.queue.schemas.queue_item import QueueCreate, QueueResponse, QueueUpdate
from modules.queue.services.queue_service import QueueService

router = APIRouter(
    prefix="/queue",
    tags=["Queue"],
)


@router.post("/", response_model=QueueResponse)
def create_queue_item(
    data: QueueCreate,
    db: Session = Depends(get_db),
):
    queue_item = QueueService.create(db, data)
    if queue_item is None:
        raise HTTPException(status_code=409, detail="Queue item already exists for this job")
    return queue_item


@router.get("/", response_model=list[QueueResponse])
def get_queue_items(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    return QueueService.get_all(db, page, page_size)


@router.patch("/{queue_id}", response_model=QueueResponse)
def update_queue_item(
    queue_id: int,
    data: QueueUpdate,
    db: Session = Depends(get_db),
):
    queue_item = QueueService.update(db, queue_id, data)
    if queue_item is None:
        raise HTTPException(status_code=404, detail="Queue item not found")
    return queue_item
