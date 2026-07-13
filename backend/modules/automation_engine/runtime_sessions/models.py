from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RuntimeSession:
    session_id: str
    account_id: str
    platform: str
    identity_id: str | None
    owner_token: str
    worker_round_id: str
    profile_path: str
    normalized_profile_path: str
    browser: Any = None
    context: Any = None
    page: Any = None
    created_at: str = field(default_factory=utc_now)
    last_used_at: str | None = None
    jobs_processed: int = 0
    healthy: bool = True
    invalidated: bool = False
    invalidated_reason: str | None = None
    close_requested: bool = False
    closed_at: str | None = None
    browser_start_duration_ms: int = 0
    last_health_check_duration_ms: int = 0
    last_prepare_duration_ms: int = 0
    last_reset_duration_ms: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        account_id: str,
        platform: str,
        identity_id: str | None = None,
        owner_token: str,
        worker_round_id: str,
        profile_path: str,
        normalized_profile_path: str | None = None,
        browser: Any = None,
        context: Any = None,
        page: Any = None,
        browser_start_duration_ms: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> "RuntimeSession":
        return cls(
            session_id=f"session_{uuid4().hex[:12]}",
            account_id=account_id,
            platform=platform,
            identity_id=identity_id,
            owner_token=owner_token,
            worker_round_id=worker_round_id,
            profile_path=profile_path,
            normalized_profile_path=normalized_profile_path or profile_path,
            browser=browser,
            context=context,
            page=page,
            browser_start_duration_ms=browser_start_duration_ms,
            metadata=metadata or {},
        )

    def safe_diagnostics(self) -> dict[str, Any]:
        durations = list(self.metadata.get("job_durations_ms") or [])
        average = int(sum(durations) / len(durations)) if durations else 0
        return {
            "session_id": self.session_id,
            "account_id": self.account_id,
            "platform": self.platform,
            "identity_id": self.identity_id,
            "worker_round_id": self.worker_round_id,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "jobs_processed": self.jobs_processed,
            "healthy": self.healthy,
            "invalidated": self.invalidated,
            "invalidated_reason": self.invalidated_reason,
            "close_requested": self.close_requested,
            "closed_at": self.closed_at,
            "browser_start_duration_ms": self.browser_start_duration_ms,
            "last_health_check_duration_ms": self.last_health_check_duration_ms,
            "last_prepare_duration_ms": self.last_prepare_duration_ms,
            "last_reset_duration_ms": self.last_reset_duration_ms,
            "average_job_duration_ms": average,
            "metadata": {
                key: value
                for key, value in self.metadata.items()
                if key
                not in {
                    "cookies",
                    "localStorage",
                    "local_storage",
                    "authorization",
                    "token",
                    "api_token",
                    "password",
                    "secret",
                }
            },
        }
