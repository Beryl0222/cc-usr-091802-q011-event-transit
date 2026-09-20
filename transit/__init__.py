"""比赛日交通权益与调度服务（事件溯源）。

模块划分：
- events:  追加式事件日志，幂等去重，双时间（事件发生时间 / 入库时间）
- clock:   赛事日时区与统一时钟，离线补传使用事件发生时间重放
- entitlements: 报名、号码布、发令批次、凭证有效窗口、免费乘车核销
- dispatch: 线路班次、车辆容量、临时管制、改派、通知回执、医疗优先
- trace:   拥堵告警沿 批次→车辆→路段→处置决定 的链路追溯
- service: 应用服务门面，聚合各域命令与只读重放查询
"""

from .clock import EVENT_TIMEZONE, GameClock
from .events import Event, EventStore
from .service import TransitService

__all__ = ["EVENT_TIMEZONE", "GameClock", "Event", "EventStore", "TransitService"]
