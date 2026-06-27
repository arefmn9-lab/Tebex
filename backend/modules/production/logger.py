import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ProductionLogger:
    backend_root = Path(__file__).resolve().parents[2]
    log_dir = backend_root / "runtime" / "logs"

    @classmethod
    def log(
        cls,
        module: str,
        event: str,
        *,
        account_id: int | str | None = None,
        platform: str | None = None,
        execution_type: str | None = None,
        level: str = "info",
        context: dict[str, Any] | None = None,
    ):
        cls.log_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": level,
            "module": module,
            "event": event,
            "account_id": account_id,
            "platform": platform,
            "execution_type": execution_type,
            "context": context or {},
        }
        path = cls.log_dir / f"{cls._module_name(module)}.jsonl"
        with path.open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(record, default=str) + "\n")
        return record

    @classmethod
    def recent(cls, module: str | None = None, limit: int = 100):
        cls.log_dir.mkdir(parents=True, exist_ok=True)
        paths = [cls.log_dir / f"{cls._module_name(module)}.jsonl"] if module else cls.log_dir.glob("*.jsonl")
        records: list[dict[str, Any]] = []

        for path in paths:
            if not path.exists():
                continue
            lines = path.read_text(encoding="utf-8").splitlines()
            for line in lines[-limit:]:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

        records.sort(key=lambda item: item.get("timestamp", ""))
        return records[-limit:]

    @classmethod
    def for_account(cls, account_id: int | str, limit: int = 100):
        return [
            record
            for record in cls.recent(limit=limit * 5)
            if str(record.get("account_id")) == str(account_id)
        ][-limit:]

    @staticmethod
    def _module_name(module: str | None):
        return str(module or "system").strip().lower().replace(" ", "_")
