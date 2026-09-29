"""仅追加的事件存储：JSONL 落盘，进程重启后靠重放恢复。"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any


class ConcurrencyError(RuntimeError):
    """并发写入越过了聚合当前版本（乐观锁冲突）。"""


class EventStore:
    """每行一条事件。同一聚合的事件 version 必须从 1 连续递增。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._events: list[dict[str, Any]] = []
        self._event_ids: set[str] = set()
        self._versions: dict[str, int] = {}
        self._reload()

    def _reload(self) -> None:
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                self._ingest(json.loads(line))

    def _ingest(self, event: dict[str, Any]) -> None:
        self._event_ids.add(event["event_id"])
        aggregate_id = event["aggregate_id"]
        current = self._versions.get(aggregate_id, 0)
        if event["version"] != current + 1:
            raise ValueError(
                f"聚合 {aggregate_id} 版本断裂：收到 v{event['version']}，当前 v{current}"
            )
        self._versions[aggregate_id] = event["version"]
        self._events.append(event)

    @property
    def events(self) -> list[dict[str, Any]]:
        """按全局写入顺序排列的全部事件（流位置即列表下标）。"""
        with self._lock:
            return list(self._events)

    def append(self, event: dict[str, Any], *, expected_version: int | None = None) -> int:
        """原子追加一条事件，返回其全局流位置。

        expected_version 给出调用方看到的该聚合最新版本：
        与重检查结果不一致时拒绝写入，防止并发批准互相覆盖。
        """
        with self._lock:
            if event["event_id"] in self._event_ids:
                raise ValueError(f"事件标识不得复用：{event['event_id']}")
            aggregate_id = event["aggregate_id"]
            current = self._versions.get(aggregate_id, 0)
            if expected_version is not None and expected_version != current:
                raise ConcurrencyError(
                    f"聚合 {aggregate_id} 已被并发推进到 v{current}（调用方基于 v{expected_version}）"
                )
            if event["version"] != current + 1:
                raise ConcurrencyError(
                    f"聚合 {aggregate_id} 版本冲突：要写 v{event['version']}，当前 v{current}"
                )
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
                handle.flush()
            self._ingest(event)
            return len(self._events) - 1

    def version_of(self, aggregate_id: str) -> int:
        with self._lock:
            return self._versions.get(aggregate_id, 0)

    def events_for(self, aggregate_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [e for e in self._events if e["aggregate_id"] == aggregate_id]
