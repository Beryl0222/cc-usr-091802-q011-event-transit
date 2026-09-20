"""只增事件日志。

一条事件同时携带两条时间轴：

* ``occurred_at`` —— 业务真正发生的时刻（闸机验票、车辆发车、道路封闭的墙钟时间）；
* ``recorded_at`` —— 平台收到并登记的时刻（离线补传时严格晚于发生时刻）。

日志只追加、不修改、不删除。任何状态都可以从日志重新折叠得到；
``dedupe_key`` 保证同一条离线记录补传多少次都只生效一次。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .clock import format_dt, now, parse
from .errors import DuplicateReplay


@dataclass(frozen=True)
class Event:
    seq: int
    event_id: str
    type: str
    occurred_at: str  # ISO，Asia/Shanghai
    recorded_at: str
    data: dict[str, Any]
    dedupe_key: str | None = None
    actor: str = "system"
    refs: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "event_id": self.event_id,
            "type": self.type,
            "occurred_at": self.occurred_at,
            "recorded_at": self.recorded_at,
            "data": self.data,
            "dedupe_key": self.dedupe_key,
            "actor": self.actor,
            "refs": list(self.refs),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Event":
        return cls(
            seq=raw["seq"],
            event_id=raw["event_id"],
            type=raw["type"],
            occurred_at=raw["occurred_at"],
            recorded_at=raw["recorded_at"],
            data=raw.get("data", {}),
            dedupe_key=raw.get("dedupe_key"),
            actor=raw.get("actor", "system"),
            refs=tuple(raw.get("refs", [])),
        )


class EventLog:
    """线程安全的只增日志，可选 JSONL 持久化。"""

    def __init__(self, path: str | Path | None = None):
        self._events: list[Event] = []
        self._by_id: dict[str, Event] = {}
        self._by_dedupe: dict[str, Event] = {}
        self._lock = threading.RLock()
        self._path = Path(path) if path else None
        if self._path and self._path.exists():
            self._load()

    # ------------------------------------------------------------------ 写入

    def append(
        self,
        event_type: str,
        data: dict[str, Any],
        *,
        occurred_at: str | None = None,
        recorded_at: str | None = None,
        dedupe_key: str | None = None,
        actor: str = "system",
        refs: Iterable[str] | None = None,
    ) -> Event:
        """追加一条事件。

        命中已有的 ``dedupe_key`` 时抛出 :class:`DuplicateReplay`，
        其中携带首次登记的原始事件——调用方据此识别"重放"，且容量绝不二次占用。
        """
        occurred = parse(occurred_at) if occurred_at else now()
        recorded = parse(recorded_at) if recorded_at else now()
        if recorded < occurred:
            # 服务器时钟再快，也不可能先于事件发生登记；防止伪造的时间线倒挂。
            recorded = occurred
        with self._lock:
            if dedupe_key is not None and dedupe_key in self._by_dedupe:
                original = self._by_dedupe[dedupe_key]
                raise DuplicateReplay(
                    f"事件 {dedupe_key} 已登记（seq={original.seq}），重放不产生第二次效果",
                    details={"event": original.to_dict()},
                )
            seq = len(self._events) + 1
            event = Event(
                seq=seq,
                event_id=f"evt-{seq:06d}",
                type=event_type,
                occurred_at=format_dt(occurred),
                recorded_at=format_dt(recorded),
                data=dict(data),
                dedupe_key=dedupe_key,
                actor=actor,
                refs=tuple(refs or ()),
            )
            self._events.append(event)
            self._by_id[event.event_id] = event
            if dedupe_key is not None:
                self._by_dedupe[dedupe_key] = event
            if self._path:
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
            return event

    # ------------------------------------------------------------------ 读取

    def all(self) -> list[Event]:
        with self._lock:
            return list(self._events)

    def get(self, event_id: str) -> Event:
        return self._by_id[event_id]

    def duplicate_of(self, dedupe_key: str) -> Event | None:
        """已存在同键事件则返回它，否则 None。用于容量检查前先做幂等短路。"""
        return self._by_dedupe.get(dedupe_key)

    def by_type(self, *event_types: str) -> list[Event]:
        wanted = set(event_types)
        return [e for e in self.all() if e.type in wanted]

    def referencing(self, ref: str) -> list[Event]:
        """所有 ``refs`` 指向某个标识（选手/车辆/路段/告警/班次…）的事件。"""
        return [e for e in self.all() if ref in e.refs]

    def __len__(self) -> int:
        return len(self._events)

    # ------------------------------------------------------------------ 持久化

    def _load(self) -> None:
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = Event.from_dict(json.loads(line))
            self._events.append(event)
            self._by_id[event.event_id] = event
            if event.dedupe_key:
                self._by_dedupe[event.dedupe_key] = event
