from __future__ import annotations

from collections import defaultdict, deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from statistics import median
from threading import Lock
from time import perf_counter
from typing import Any, Iterator


@dataclass
class RequestTiming:
    route: str
    started: float = field(default_factory=perf_counter)
    db_query_count: int = 0
    db_total_ms: float = 0.0
    spans: dict[str, float] = field(default_factory=dict)


_current: ContextVar[RequestTiming | None] = ContextVar("clinicos_request_timing", default=None)
_recent: deque[dict[str, Any]] = deque(maxlen=1000)
_lock = Lock()


def begin(route: str) -> object:
    return _current.set(RequestTiming(route=route))


def finish(token: object, *, status_code: int = 200, timed_out: bool = False) -> None:
    timing = _current.get()
    if timing is not None:
        total_ms = (perf_counter() - timing.started) * 1000
        slowest = max(timing.spans.items(), key=lambda item: item[1], default=("request", total_ms))
        record = {
            "route": timing.route,
            "duration_ms": round(total_ms, 3),
            "status_code": status_code,
            "timeout": bool(timed_out),
            "db_query_count": timing.db_query_count,
            "db_total_duration_ms": round(timing.db_total_ms, 3),
            "slowest_span": {"name": slowest[0], "duration_ms": round(slowest[1], 3)},
            "spans": {key: round(value, 3) for key, value in timing.spans.items()},
        }
        with _lock:
            _recent.append(record)
    _current.reset(token)


@contextmanager
def span(name: str) -> Iterator[None]:
    started = perf_counter()
    try:
        yield
    finally:
        timing = _current.get()
        if timing is not None:
            timing.spans[name] = timing.spans.get(name, 0.0) + (perf_counter() - started) * 1000


def record_db_query(duration_ms: float) -> None:
    timing = _current.get()
    if timing is not None:
        timing.db_query_count += 1
        timing.db_total_ms += duration_ms


def report() -> dict[str, Any]:
    with _lock:
        records = list(_recent)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in records:
        grouped[item["route"]].append(item)
    routes = []
    for route, items in sorted(grouped.items()):
        durations = sorted(float(item["duration_ms"]) for item in items)
        p95_index = max(0, min(len(durations) - 1, int((len(durations) - 1) * 0.95)))
        slowest = max(items, key=lambda item: float(item["slowest_span"]["duration_ms"]))
        routes.append({
            "route": route,
            "count": len(items),
            "p50_ms": round(median(durations), 3),
            "p95_ms": round(durations[p95_index], 3),
            "max_ms": round(max(durations), 3),
            "timeout_count": sum(bool(item["timeout"]) for item in items),
            "db_query_count": sum(int(item["db_query_count"]) for item in items),
            "db_total_duration_ms": round(sum(float(item["db_total_duration_ms"]) for item in items), 3),
            "slowest_span": slowest["slowest_span"],
        })
    return {"status": "ok", "sample_count": len(records), "routes": routes}
