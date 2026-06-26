from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from core.database.dependencies import get_db
from modules.telegram.exceptions import (
    ConnectionLost,
    FloodWait,
    InvalidSession,
    RateLimited,
    UserNotFound,
)
from modules.telegram.schemas.telegram import (
    TelegramAccountResponse,
    TelegramConnectRequest,
    TelegramDisconnectRequest,
    TelegramHealthResponse,
    TelegramSendRequest,
    TelegramSendResponse,
    TelegramStatusResponse,
)
from modules.telegram.services.health import TelegramHealthService
from modules.telegram.services.telegram_adapter import TelegramAdapter

router = APIRouter(
    prefix="/telegram",
    tags=["Telegram"],
)


@router.post("/connect", response_model=TelegramAccountResponse)
def connect(
    data: TelegramConnectRequest,
    db: Session = Depends(get_db),
):
    try:
        adapter = TelegramAdapter()
        return adapter.connect(
            db=db,
            communication_account_id=data.communication_account_id,
            phone_number=data.phone_number,
            session_name=data.session_name,
        )
    except InvalidSession as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/disconnect", response_model=TelegramAccountResponse)
def disconnect(
    data: TelegramDisconnectRequest,
    db: Session = Depends(get_db),
):
    try:
        adapter = TelegramAdapter()
        return adapter.disconnect(db, data.communication_account_id)
    except InvalidSession as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/send", response_model=TelegramSendResponse)
def send_message(
    data: TelegramSendRequest,
    db: Session = Depends(get_db),
):
    try:
        adapter = TelegramAdapter()
        return adapter.send_message(
            db=db,
            communication_account_id=data.communication_account_id,
            target=data.target,
            text=data.text,
        )
    except InvalidSession as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ConnectionLost as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except UserNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (FloodWait, RateLimited) as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc


@router.get("/status", response_model=TelegramStatusResponse)
def get_status(
    communication_account_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
):
    adapter = TelegramAdapter()
    return adapter.status(db, communication_account_id)


@router.get("/health", response_model=TelegramHealthResponse)
def get_health(
    communication_account_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
):
    return TelegramHealthService.get_health(db, communication_account_id)
