"""统一时区与时间解析。

所有业务时间都以 **Asia/Shanghai（UTC+08:00，中国不实行夏令时）** 为准。
事件同时保存：

* ``occurred_at`` —— 业务发生时间（本地墙钟时间，即闸机/车载设备当时看到的时间）；
* ``recorded_at`` —— 平台登记时间（补传时晚于发生时间）。

离线补传、"效力只向未来生效"、按原始时区重放，全部依赖这两条时间轴。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

EVENT_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
TZ_NAME = "Asia/Shanghai"


def now() -> datetime:
    """返回当前的赛事本地时间（带时区的 aware datetime）。"""
    return datetime.now(EVENT_TZ)


def parse(value: str | datetime) -> datetime:
    """把 ``YYYY-MM-DDTHH:MM:SS+08:00`` 之类的字符串解析为 aware datetime。

    不带时区后缀的时间一律按赛事本地时区解释——设备离线写出的本地墙钟
    时间正是这种形态，绝不能按服务器所在时区另作猜测。
    """
    if isinstance(value, datetime):
        dt = value
    else:
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=EVENT_TZ)
    return dt.astimezone(EVENT_TZ)


def format_dt(dt: datetime) -> str:
    """以带偏移量的 ISO 字符串输出，例如 ``2026-09-20T05:30:00+08:00``。"""
    return parse(dt).isoformat(timespec="seconds")


def between(dt: datetime, start: datetime | None, end: datetime | None) -> bool:
    """半开区间 ``[start, end)`` 判定；端点为空表示不限制。"""
    dt = parse(dt)
    if start is not None and dt < parse(start):
        return False
    if end is not None and dt >= parse(end):
        return False
    return True
