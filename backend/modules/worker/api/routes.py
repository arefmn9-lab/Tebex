from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from core.database.dependencies import get_db
from modules.worker.schemas.worker import WorkerCreate, WorkerResponse, WorkerUpdate
from modules.worker.services.worker_service import WorkerService

router = APIRouter(
    prefix="/workers",
    tags=["Workers"],
)


@router.post("/", response_model=WorkerResponse)
def create_worker(
    data: WorkerCreate,
    db: Session = Depends(get_db),
):
    return WorkerService.create(db, data)


@router.get("/", response_model=list[WorkerResponse])
def get_workers(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    return WorkerService.get_all(db, page, page_size)


@router.patch("/{worker_id}", response_model=WorkerResponse)
def update_worker(
    worker_id: int,
    data: WorkerUpdate,
    db: Session = Depends(get_db),
):
    worker = WorkerService.update(db, worker_id, data)
    if worker is None:
        raise HTTPException(status_code=404, detail="Worker not found")
    return worker
