from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from .models import MessageSource


SOURCE_TYPES = {"channel", "group", "chat", "link"}
MESSAGE_REF_TYPES = {"latest", "pinned", "specific"}


def runtime_path() -> Path:
    return Path(__file__).resolve().parents[3] / "runtime" / "message_sources.json"


class MessageSourceStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or runtime_path()

    def list_sources(self) -> list[dict[str, Any]]:
        return [self._normalize_source(item).to_dict() for item in self._read_json([])]

    def list_platform_sources(self, platform_id: str) -> list[dict[str, Any]]:
        return [item for item in self.list_sources() if item["platform_id"] == platform_id]

    def get_source(self, message_source_id: str) -> dict[str, Any] | None:
        for item in self.list_sources():
            if item["message_source_id"] == message_source_id:
                return item
        return None

    def create_source(self, payload: dict[str, Any]) -> dict[str, Any]:
        sources = self.list_sources()
        source_id = str(payload.get("message_source_id") or "").strip()
        if not source_id:
            source_id = _slug(str(payload.get("platform_id") or "platform"), str(payload.get("name") or "source"))
        if any(item["message_source_id"] == source_id for item in sources):
            raise ValueError(f"Message source already exists: {source_id}")
        source = self._normalize_source({**payload, "message_source_id": source_id}).to_dict()
        sources.append(source)
        self._write_json(sources)
        return source

    def update_source(self, message_source_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        sources = self.list_sources()
        for index, item in enumerate(sources):
            if item["message_source_id"] == message_source_id:
                updated = self._normalize_source({**item, **payload, "message_source_id": message_source_id}).to_dict()
                sources[index] = updated
                self._write_json(sources)
                return updated
        raise KeyError(message_source_id)

    def _normalize_source(self, payload: dict[str, Any]) -> MessageSource:
        source_type = str(payload.get("source_type") or "channel")
        if source_type not in SOURCE_TYPES:
            source_type = "channel"
        message_ref_type = str(payload.get("message_ref_type") or "latest")
        if message_ref_type not in MESSAGE_REF_TYPES:
            message_ref_type = "latest"
        return MessageSource(
            message_source_id=str(payload.get("message_source_id") or ""),
            platform_id=str(payload.get("platform_id") or "bale"),
            name=str(payload.get("name") or "Message Source"),
            campaign_tag=str(payload.get("campaign_tag") or ""),
            source_type=source_type,
            source_ref=str(payload.get("source_ref") or ""),
            message_ref_type=message_ref_type,
            message_ref_value=str(payload.get("message_ref_value") or ""),
            enabled=bool(payload.get("enabled", True)),
            notes=str(payload.get("notes") or ""),
        )

    def _read_json(self, default: Any) -> Any:
        try:
            with self.path.open("r", encoding="utf-8") as json_file:
                return json.load(json_file)
        except Exception:
            return deepcopy(default)

    def _write_json(self, data: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as json_file:
            json.dump(data, json_file, ensure_ascii=False, indent=2)


def _slug(prefix: str, value: str) -> str:
    safe = "".join(char.lower() if char.isalnum() else "_" for char in value).strip("_")
    return f"{prefix}_{safe or 'source'}"


message_source_store = MessageSourceStore()
