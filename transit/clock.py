"""统一时钟与赛事日时区。

所有业务时间以赛事当地时区（Asia/Shanghai，UTC+8）解释和展示，
但底层一律存为带时区的 datetime；离线设备补传时携带的本地时间
也按此时区还原，保证“按原始时区重放”。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

EVENT_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")


class GameClock:
    """可驱动的时钟：测试与历史重放时可显式给定“现在”。

    now() 返回带时区的当前时刻；没有注入固定时间时走系统时钟。
    """

    def __init__(self, fixed: datetime | None = None):
        self._fixed = self._as_aware(fixed) if fixed is not None else None
        self._override_now: datetime | None = None

    @staticmethod
    def _as_aware(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=EVENT_TIMEZONE)
        return value.astimezone(EVENT_TIMEZONE)

    @property
    def now(self) -> datetime:
        if self._override_now is not None:
            return self._override_now
        if self._fixed is not None:
            return self._fixed
        return datetime.now(EVENT_TIMEZONE)

    def travel(self, value: datetime) -> None:
        """把时钟拨到指定时刻（用于逐时刻重放）。"""
        self._override_now = self._as_aware(value)

    def reset(self) -> None:
        self._override_now = None

    @staticmethod
    def local(value: datetime) -> datetime:
        """转成赛事时区的本地时间（展示用）。"""
        return TransitAware.ensure(value).astimezone(EVENT_TIMEZONE)


class TransitAware:
    """时间归一化小工具。"""

    @staticmethod
    def ensure(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=EVENT_TIMEZONE)
        return value.astimezone(EVENT_TIMEZONE)


def parse_event_time(raw: str | datetime) -> datetime:
    """把设备/接口传入的时间解析为赛事时区 aware datetime。

    接受：带偏移量的 ISO 串（2026-09-20T05:30:00+08:00）、
    无时区的 ISO 串（按 Asia/Shanghai 解释）、datetime。
    """
    if isinstance(raw, datetime):
        value = raw
    else:
        text = raw.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        value = datetime.fromisoformat(text)
    return TransitAware.ensure(value)
