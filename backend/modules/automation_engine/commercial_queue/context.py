from __future__ import annotations

from dataclasses import asdict, dataclass
from uuid import uuid4


@dataclass(frozen=True)
class OperationContext:
    correlation_id: str
    scheduler_tick_id: str | None = None
    worker_round_id: str | None = None
    job_id: str | None = None
    campaign_id: str | None = None
    recipient_id: str | None = None
    account_id: str | None = None
    platform: str = "bale"
    session_id: str | None = None

    @classmethod
    def create(cls, **kwargs: object) -> "OperationContext":
        return cls(correlation_id=str(kwargs.pop("correlation_id", "") or f"corr_{uuid4().hex[:12]}"), **kwargs)

    def child(self, **kwargs: object) -> "OperationContext":
        data = asdict(self)
        data.update(kwargs)
        if not data.get("correlation_id"):
            data["correlation_id"] = f"corr_{uuid4().hex[:12]}"
        return OperationContext(**data)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)
