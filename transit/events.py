"""追加式事件日志。

核心约定：
- 只追加、不修改、不删除（挂失/退赛/改枪都以新事件表达）。
- 每条事件带双时间：event_time（业务发生时刻，离线设备可补传）与
  recorded_at（服务端入库时刻），全部带时区。
- event_id 全局幂等：同一记录重传/重放返回既有事件，绝不产生第二条，
  因此容量不会被同一笔核销重复占用。
- 可落盘为 JSONL；按 event_time（原始时间线）重放，支持“回到任一时刻”。
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from .clock import EVENT_TIMEZONE, parse_event_time


@dataclass(frozen=True)
class Event:
    seq: int
    event_id: str
    event_type: str
    event_time: datetime
    recorded_at: datetime
    payload: dict = field(default_factory=dict)
    device_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "event_time": self.event_time.isoformat(),
            "recorded_at": self.recorded_at.isoformat(),
            "payload": self.payload,
            "device_id": self.device_id,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Event":
        return cls(
            seq=int(raw["seq"]),
            event_id=raw["event_id"],
            event_type=raw["event_type"],
            event_time=parse_event_time(raw["event_time"]),
            recorded_at=parse_event_time(raw["recorded_at"]),
            payload=raw.get("payload", {}),
            device_id=raw.get("device_id"),
        )


class EventStore:
    """线程安全的追加日志，可选 JSONL 持久化。"""

    def __init__(self, path: str | Path | None = None):
        self._lock = threading.RLock()
        self._events: list[Event] = []
        self._index: dict[str, Event] = {}
        self._path = Path(path) if path else None
        if self._path and self._path.exists():
            self._load()

    # ---- 写入 -----------------------------------------------------------

    def append(
        self,
        event_type: str,
        payload: dict,
        event_time: datetime | str,
        event_id: str | None = None,
        device_id: str | None = None,
        recorded_at: datetime | None = None,
    ) -> tuple[Event, bool]:
        """追加事件。

        返回 (事件, 是否新建)。event_id 已存在时原样返回既有事件，
        False 表示本次是重复提交——投影不会再处理它。
        """
        event_id = event_id or str(uuid.uuid4())
        when = parse_event_time(event_time)
        with self._lock:
            existing = self._index.get(event_id)
            if existing is not None:
                return existing, False
            stored_at = (
                parse_event_time(recorded_at) if recorded_at else datetime.now(EVENT_TIMEZONE)
            )
            event = Event(
                seq=len(self._events),
                event_id=event_id,
                event_type=event_type,
                event_time=when,
                recorded_at=stored_at,
                payload=payload,
                device_id=device_id,
            )
            self._events.append(event)
            self._index[event_id] = event
            if self._path:
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
            return event, True

    # ---- 读取 -----------------------------------------------------------

    def all(self) -> list[Event]:
        with self._lock:
            return list(self._events)

    def get(self, event_id: str) -> Event | None:
        with self._lock:
            return self._index.get(event_id)

    def replay(
        self,
        as_of: datetime | str | None = None,
        types: Iterable[str] | None = None,
        *,
        order: str = "event_time",
        predicate: Callable[[Event], bool] | None = None,
    ) -> list[Event]:
        """按时间线重放事件。

        as_of 给出时刻时，只包含 event_time <= as_of 的事件（离线补传
        的历史事件也落在其原始时刻上）。order="event_time" 按业务时间，
        order="recorded" 按入库顺序。
        """
        cutoff = parse_event_time(as_of) if as_of else None
        wanted = set(types) if types else None
        with self._lock:
            events = list(self._events)
        if order == "event_time":
            events.sort(key=lambda e: (e.event_time, e.seq))
        else:
            events.sort(key=lambda e: e.seq)
        result = []
        for event in events:
            if cutoff is not None and event.event_time > cutoff:
                continue
            if wanted is not None and event.event_type not in wanted:
                continue
            if predicate is not None and not predicate(event):
                continue
            result.append(event)
        return result

    # ---- 持久化 ---------------------------------------------------------

    def _load(self) -> None:
        assert self._path is not None
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            event = Event.from_dict(json.loads(line))
            self._events.append(event)
            self._index[event.event_id] = event
