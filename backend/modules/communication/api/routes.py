from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from core.database.dependencies import get_db
from modules.communication.schemas.account import (
    AccountCreate,
    AccountResponse,
    AccountUpdate,
)
from modules.communication.schemas.conversation import (
    ConversationCreate,
    ConversationResponse,
    ConversationUpdate,
)
from modules.communication.schemas.job import JobCreate, JobResponse, JobUpdate
from modules.communication.schemas.message import (
    MessageCreate,
    MessageResponse,
    MessageUpdate,
)
from modules.communication.schemas.platform import (
    PlatformCreate,
    PlatformResponse,
    PlatformUpdate,
)
from modules.communication.services.account_service import AccountService
from modules.communication.services.conversation_service import ConversationService
from modules.communication.services.job_service import JobService
from modules.communication.services.message_service import MessageService
from modules.communication.services.platform_service import PlatformService

router = APIRouter(
    prefix="/communication",
    tags=["Communication"],
)


@router.post("/platforms/", response_model=PlatformResponse)
def create_platform(
    data: PlatformCreate,
    db: Session = Depends(get_db),
):
    platform = PlatformService.create(db, data)

    if platform is None:
        raise HTTPException(status_code=409, detail="Platform code already exists")

    return platform


@router.get("/platforms/", response_model=list[PlatformResponse])
def get_platforms(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    return PlatformService.get_all(db, page, page_size)


@router.get("/platforms/{platform_id}", response_model=PlatformResponse)
def get_platform(
    platform_id: int,
    db: Session = Depends(get_db),
):
    platform = PlatformService.get_by_id(db, platform_id)

    if platform is None:
        raise HTTPException(status_code=404, detail="Platform not found")

    return platform


@router.patch("/platforms/{platform_id}", response_model=PlatformResponse)
def update_platform(
    platform_id: int,
    data: PlatformUpdate,
    db: Session = Depends(get_db),
):
    platform = PlatformService.update(db, platform_id, data)

    if platform is None:
        raise HTTPException(status_code=404, detail="Platform not found")

    return platform


@router.delete("/platforms/{platform_id}")
def delete_platform(
    platform_id: int,
    db: Session = Depends(get_db),
):
    platform = PlatformService.delete(db, platform_id)

    if platform is None:
        raise HTTPException(status_code=404, detail="Platform not found")

    return {"message": "Platform deleted successfully"}


@router.post("/accounts/", response_model=AccountResponse)
def create_account(
    data: AccountCreate,
    db: Session = Depends(get_db),
):
    return AccountService.create(db, data)


@router.get("/accounts/", response_model=list[AccountResponse])
def get_accounts(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    return AccountService.get_all(db, page, page_size)


@router.get("/accounts/{account_id}", response_model=AccountResponse)
def get_account(
    account_id: int,
    db: Session = Depends(get_db),
):
    account = AccountService.get_by_id(db, account_id)

    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")

    return account


@router.patch("/accounts/{account_id}", response_model=AccountResponse)
def update_account(
    account_id: int,
    data: AccountUpdate,
    db: Session = Depends(get_db),
):
    account = AccountService.update(db, account_id, data)

    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")

    return account


@router.delete("/accounts/{account_id}")
def delete_account(
    account_id: int,
    db: Session = Depends(get_db),
):
    account = AccountService.delete(db, account_id)

    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")

    return {"message": "Account deleted successfully"}


@router.post("/conversations/", response_model=ConversationResponse)
def create_conversation(
    data: ConversationCreate,
    db: Session = Depends(get_db),
):
    return ConversationService.create(db, data)


@router.get("/conversations/", response_model=list[ConversationResponse])
def get_conversations(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    return ConversationService.get_all(db, page, page_size)


@router.get("/conversations/{conversation_id}", response_model=ConversationResponse)
def get_conversation(
    conversation_id: int,
    db: Session = Depends(get_db),
):
    conversation = ConversationService.get_by_id(db, conversation_id)

    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    return conversation


@router.patch("/conversations/{conversation_id}", response_model=ConversationResponse)
def update_conversation(
    conversation_id: int,
    data: ConversationUpdate,
    db: Session = Depends(get_db),
):
    conversation = ConversationService.update(db, conversation_id, data)

    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    return conversation


@router.delete("/conversations/{conversation_id}")
def delete_conversation(
    conversation_id: int,
    db: Session = Depends(get_db),
):
    conversation = ConversationService.delete(db, conversation_id)

    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    return {"message": "Conversation deleted successfully"}


@router.post("/messages/", response_model=MessageResponse)
def create_message(
    data: MessageCreate,
    db: Session = Depends(get_db),
):
    return MessageService.create(db, data)


@router.get("/messages/", response_model=list[MessageResponse])
def get_messages(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    return MessageService.get_all(db, page, page_size)


@router.get("/messages/{message_id}", response_model=MessageResponse)
def get_message(
    message_id: int,
    db: Session = Depends(get_db),
):
    message = MessageService.get_by_id(db, message_id)

    if message is None:
        raise HTTPException(status_code=404, detail="Message not found")

    return message


@router.patch("/messages/{message_id}", response_model=MessageResponse)
def update_message(
    message_id: int,
    data: MessageUpdate,
    db: Session = Depends(get_db),
):
    message = MessageService.update(db, message_id, data)

    if message is None:
        raise HTTPException(status_code=404, detail="Message not found")

    return message


@router.delete("/messages/{message_id}")
def delete_message(
    message_id: int,
    db: Session = Depends(get_db),
):
    message = MessageService.delete(db, message_id)

    if message is None:
        raise HTTPException(status_code=404, detail="Message not found")

    return {"message": "Message deleted successfully"}


@router.post("/jobs/", response_model=JobResponse)
def create_job(
    data: JobCreate,
    db: Session = Depends(get_db),
):
    return JobService.create(db, data)


@router.get("/jobs/", response_model=list[JobResponse])
def get_jobs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    return JobService.get_all(db, page, page_size)


@router.get("/jobs/{job_id}", response_model=JobResponse)
def get_job(
    job_id: int,
    db: Session = Depends(get_db),
):
    job = JobService.get_by_id(db, job_id)

    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    return job


@router.patch("/jobs/{job_id}", response_model=JobResponse)
def update_job(
    job_id: int,
    data: JobUpdate,
    db: Session = Depends(get_db),
):
    job = JobService.update(db, job_id, data)

    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    return job


@router.delete("/jobs/{job_id}")
def delete_job(
    job_id: int,
    db: Session = Depends(get_db),
):
    job = JobService.delete(db, job_id)

    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    return {"message": "Job deleted successfully"}
