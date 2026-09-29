"""追加式事件存储：JSONL 持久化与回放。"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from .envelope import validate_event
from .errors import DomainError


class EventStore:
    """按聚合维护流版本的追加式事件日志。

    事件一旦写入不得修改；业务修订通过新事件表达。提供 ``path`` 时
    每条事件追加为一行 JSON，重启后从文件回放，未完成的流程可继续。
    """

    def __init__(self, path: str | Path | None = None, clock=None):
        self._path = Path(path) if path is not None else None
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._events: list[dict] = []
        self._stream_versions: dict[tuple[str, str], int] = {}
        if self._path and self._path.exists():
            self._load()

    @property
    def clock(self):
        return self._clock

    def _load(self) -> None:
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            event = json.loads(line)
            errors = validate_event(event)
            if errors:
                raise DomainError(f"事件日志损坏：{'；'.join(errors)}")
            self._register(event)

    def _register(self, event: dict) -> None:
        self._events.append(event)
        key = (event["aggregate_type"], event["aggregate_id"])
        self._stream_versions[key] = event["version"]

    def append(self, event_type: str, aggregate_type: str, aggregate_id: str, summary: str, **payload) -> dict:
        with self._lock:
            key = (aggregate_type, aggregate_id)
            version = self._stream_versions.get(key, 0) + 1
            event = {
                "event_id": f"evt-{len(self._events) + 1:06d}",
                "event_type": event_type,
                "aggregate_type": aggregate_type,
                "aggregate_id": aggregate_id,
                "occurred_at": self._clock().isoformat(),
                "version": version,
                "summary": summary,
                **payload,
            }
            errors = validate_event(event)
            if errors:
                raise DomainError("；".join(errors))
            self._register(event)
            if self._path:
                with self._path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event, ensure_ascii=False) + "\n")
            return event

    def events(self) -> list[dict]:
        return list(self._events)
