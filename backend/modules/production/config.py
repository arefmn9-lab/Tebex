import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ProductionConfig:
    environment: str = os.getenv("CLINICOS_ENV", "dev")
    max_retries_per_job: int = int(os.getenv("CLINICOS_MAX_RETRIES", "4"))
    execution_timeout_seconds: int = int(os.getenv("CLINICOS_EXECUTION_TIMEOUT", "30"))
    browser_timeout_seconds: int = int(os.getenv("CLINICOS_BROWSER_TIMEOUT", "60"))
    safe_mode: bool = os.getenv("CLINICOS_SAFE_MODE", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


config = ProductionConfig()
