"""应用服务门面：聚合权益域与调度域，提供命令与只读重放查询。

另含 build_demo()：太原马拉松四枪发令的可运行样例场景，
覆盖提前运营、免费核销、离线补传、改枪/挂失/退赛、满员与拥堵告警、
道路封闭改派、通知回执、医疗转运与绿色通道全链路。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from . import dispatch as disp
from . import entitlements as ent
from .clock import EVENT_TIMEZONE, parse_event_time
from .dispatch import DispatchDomain, build_dispatch_state, capacity_snapshot, undelivered_notifications
from .entitlements import EntitlementDomain, build_entitlement_state, list_redemptions
from .events import EventStore
from .trace import list_alerts, trace_alert


class TransitService:
    def __init__(self, store: EventStore | None = None):
        self.store = store or EventStore()
        self.entitlements = EntitlementDomain(self.store)
        self.dispatch = DispatchDomain(self.store)

    # ---- 重放查询（全部可指定 as_of，按原始时区解释） -------------------

    def replay_capacity(self, as_of=None) -> list[dict]:
        return capacity_snapshot(self.store, as_of)

    def replay_redemptions(self, as_of=None) -> list[dict]:
        return list_redemptions(self.store, as_of)

    def replay_undelivered(self, as_of=None) -> list[dict]:
        return undelivered_notifications(self.store, as_of)

    def replay_entitlements(self, as_of=None) -> dict:
        state = build_entitlement_state(self.store, as_of)
        return {
            "as_of": _iso(as_of),
            "waves": list(state["waves"].values()),
            "runners": list(state["runners"].values()),
            "bibs": list(state["bibs"].values()),
            "windows": list(state["windows"].values()),
        }

    def replay_dispatch(self, as_of=None) -> dict:
        state = build_dispatch_state(self.store, as_of)
        return {
            "as_of": _iso(as_of),
            "routes": list(state["routes"].values()),
            "vehicles": list(state["vehicles"].values()),
            "hospitals": list(state["hospitals"].values()),
            "trips": [
                {**{k: v for k, v in trip.items() if k != "occupied"},
                 "occupied": sorted(trip["occupied"])}
                for trip in state["trips"].values()
            ],
            "active_restrictions": list(state["active_restrictions"].values()),
            "medical": [
                {"request_id": rid, **{k: (v.isoformat() if isinstance(v, datetime) else v)
                                       for k, v in m.items()}}
                for rid, m in state["medical"].items()
            ],
        }

    def alerts(self, as_of=None) -> list[dict]:
        return list_alerts(self.store, as_of)

    def trace(self, alert_id: str, as_of=None) -> dict:
        return trace_alert(self.store, alert_id, as_of)

    def events(self, as_of=None) -> list[dict]:
        return [e.to_dict() for e in self.store.replay(as_of)]


def _iso(value) -> str | None:
    if value is None:
        return None
    return parse_event_time(value).isoformat()


# ---------------------------------------------------------------------------
# 演示场景
# ---------------------------------------------------------------------------

def build_demo(path: str | Path | None = None) -> TransitService:
    """构建四枪发令比赛日的完整样例（确定性事件编号，可重复执行）。"""
    svc = TransitService(EventStore(path))
    if svc.store.all():
        return svc  # 已持久化的场景不重复播种

    e, d = svc.entitlements, svc.dispatch
    day = "2026-09-20"

    def t(hm):  # 赛事本地时间
        return parse_event_time(f"{day}T{hm}:00+08:00")

    # ---- 四枪发令批次（赛前登记，start_at 为各枪发令时刻） -------------
    configured = parse_event_time("2026-09-18T09:00:00+08:00")
    for wave_id, label, hm in [
        ("A", "全程马拉松（第一枪）", "07:00"),
        ("B", "半程马拉松（第二枪）", "07:15"),
        ("C", "迷你跑（第三枪）", "07:30"),
        ("D", "欢乐跑（第四枪）", "07:45"),
    ]:
        e.configure_wave(wave_id, label, t(hm), configured_at=configured,
                         wave_idem=f"wave:{wave_id}")

    # ---- 报名与号码布 ----------------------------------------------------
    registrations = [
        ("R001", "A"), ("R002", "A"), ("R003", "C"), ("R004", "B"),
        ("R005", "C"), ("R006", "D"),
    ]
    for runner_id, wave_id in registrations:
        e.register_runner(runner_id, wave_id,
                          parse_event_time("2026-09-18T10:00:00+08:00"),
                          event_id=f"reg:{runner_id}")
    bibs = [("A0001", "R001"), ("A0002", "R002"), ("C0003", "R003"),
            ("B0004", "R004"), ("C0005", "R005"), ("D0006", "R006")]
    for bib, runner_id in bibs:
        e.issue_bib(runner_id, bib,
                    parse_event_time("2026-09-19T15:00:00+08:00"),
                    event_id=f"bib:{bib}")

    # ---- 免费乘车 / 提前运营窗口 ----------------------------------------
    e.open_window({"window_id": "metro-early", "mode": "metro",
                   "title": "地铁提前运营（凭号码布免费进站）",
                   "start_at": t("04:30"), "end_at": t("09:30"),
                   "note": "全线网提前至04:30"}, at=t("04:00"),
                  event_id="win:metro")
    e.open_window({"window_id": "bib-bus", "mode": "bus",
                   "title": "比赛日公交（凭号码布免费）",
                   "start_at": t("04:30"), "end_at": t("14:00")},
                  at=t("04:00"), event_id="win:bus")
    e.open_window({"window_id": "post-shuttle", "mode": "shuttle",
                   "title": "赛后疏散接驳",
                   "start_at": t("09:30"), "end_at": t("15:00"),
                   "waves": ["A", "B"]}, at=t("04:00"),
                  event_id="win:shuttle")

    # ---- 线路、替代路线、车辆、医院 -------------------------------------
    d.register_route("BUS81", "81路赛时保障线", "bus",
                     ["起点枢纽", "滨河路口", "迎泽桥西", "医疗点", "终点集散区"],
                     t("03:00"), event_id="route:bus81")
    d.register_route("BUS99", "99路临时绕行线", "bus",
                     ["起点枢纽", "北中环桥", "漪汾桥", "终点集散区"],
                     t("03:00"), event_id="route:bus99")
    d.register_alternative("BUS81:1", "BUS99", "滨河路口封闭时经北中环桥绕行",
                           t("03:00"), event_id="alt:binhe")
    d.register_route("SHUTTLE2", "赛后地铁接驳2号线", "shuttle",
                     ["终点集散区", "地铁接驳站", "太原南站"],
                     t("03:00"), event_id="route:shuttle2")

    d.register_vehicle("V-BUS-1", 3, t("03:00"), label="大巴1号", event_id="veh:1")
    d.register_vehicle("V-BUS-2", 45, t("03:00"), label="大巴2号", event_id="veh:2")
    d.register_vehicle("V-BUS-3", 45, t("03:00"), label="大巴3号", event_id="veh:3")
    d.register_vehicle("V-BUS-4", 40, t("03:00"), label="接驳4号", event_id="veh:4")
    d.register_vehicle("MED-1", 2, t("03:00"), medical=True, label="救护车1号",
                       event_id="veh:med1")
    d.register_vehicle("MED-2", 2, t("03:00"), medical=True, label="救护车2号",
                       event_id="veh:med2")
    d.register_hospital("H1", "山西医科大学第一医院", t("03:00"), event_id="hosp:1")

    # ---- 班次（凌晨排班，departure_at 为计划发车时刻） ------------------
    planned = t("03:00")
    d.schedule_trip("T1", "BUS81", "V-BUS-1", t("06:40"), ["A", "B"],
                    note="第一、二枪选手前置接驳", event_id="trip:T1",
                    scheduled_at=planned)
    d.schedule_trip("T2", "BUS81", "V-BUS-2", t("06:55"), ["A", "B"],
                    event_id="trip:T2", scheduled_at=planned)
    d.schedule_trip("T3", "BUS81", "V-BUS-3", t("07:20"), ["C"],
                    note="第三枪接驳", event_id="trip:T3", scheduled_at=planned)
    d.schedule_trip("T4", "SHUTTLE2", "V-BUS-4", t("10:30"), ["A", "B"],
                    note="赛后疏散", event_id="trip:T4", scheduled_at=planned)
    d.schedule_trip("TMED", "BUS81", "MED-2", t("07:05"), [],
                    note="医疗转运预置班（不接普通乘客）", event_id="trip:tmed",
                    scheduled_at=planned)

    # T1 满员：3 人登乘成功，第 4 人被拒并自动产生满员告警
    for i, (bib, hm) in enumerate([("A0001", "06:36"), ("A0002", "06:37"),
                                   ("B0004", "06:38")]):
        d.board("T1", bib, t(hm), f"seat:T1:{i}", stop_id="起点枢纽")
    d.board("T1", "C0003", t("06:39"), "seat:T1:overflow", stop_id="起点枢纽")
    d.mark_departed("T1", t("06:40"), event_id="dep:T1")
    d.board("T2", "A0001", t("06:50"), "seat:T2:0", stop_id="起点枢纽")
    d.mark_departed("T2", t("06:55"), event_id="dep:T2")

    # 普通选手不能占用医疗转运资源
    d.board("TMED", "D0006", t("07:02"), "seat:tmed:0", stop_id="医疗点")

    # ---- 改枪（R003: C→B，06:00 生效；此前的地铁核销仍挂第三枪） -------
    e.redeem("C0003", "metro-early", t("05:30"),
             redeem_id="ride:C0003:metro1", device_id="GATE-7")
    e.change_wave("R003", "B", t("06:00"), event_id="wavechg:R003")
    e.redeem("C0003", "metro-early", t("06:20"),
             redeem_id="ride:C0003:metro2", device_id="GATE-7")

    # ---- 断网闸机：04:25 下发快照，05:10/06:50/07:20 本地验票 ----------
    e.issue_snapshot("GATE-7", t("04:25"), event_id="snap:gate7")
    offline_scans = [
        {"redeem_id": "off:A0002:1", "bib": "A0002", "window_id": "metro-early",
         "scanned_at": t("05:10"), "device_result": "accepted"},
        {"redeem_id": "off:A0002:1", "bib": "A0002", "window_id": "metro-early",
         "scanned_at": t("05:10"), "device_result": "accepted"},  # 本地重放
        {"redeem_id": "off:A0002:2", "bib": "A0002", "window_id": "metro-early",
         "scanned_at": t("06:50"), "device_result": "accepted"},
    ]

    # ---- 挂失与退赛 ------------------------------------------------------
    e.report_lost("B0004", t("07:00"), event_id="lost:B0004")
    # 断网设备持旧快照，在挂失后仍放行一次：通行保留，服务端比对出差异
    offline_scans.append(
        {"redeem_id": "off:B0004:1", "bib": "B0004", "window_id": "metro-early",
         "scanned_at": t("07:20"), "device_result": "accepted",
         "snapshot_id": "snap:gate7"})
    e.upload_offline_scans("GATE-7", offline_scans)
    e.issue_replacement("R004", "B1004", t("07:40"), event_id="repl:B0004")

    e.redeem("B0004", "bib-bus", t("06:10"),
             redeem_id="ride:B0004:bus1")  # 挂失前已完成，保留
    e.redeem("B1004", "bib-bus", t("07:50"),
             redeem_id="ride:B1004:bus1")  # 补领后可继续
    e.withdraw("R004", t("08:00"), event_id="wd:R004")
    e.redeem("B1004", "bib-bus", t("08:30"),
             redeem_id="ride:B1004:bus2")  # 退赛后拒绝

    # ---- 拥堵告警（滨河路口），随后道路封闭 -----------------------------
    d.raise_alert("cong-BINHE", "congestion", t("07:08"), severity="high",
                  detail="滨河路口车流饱和，选手接驳受阻",
                  segment_id="BUS81:1", trip_id="T2", event_id="alert:congbinhe")

    result = d.impose_closure(
        "R-918", "BUS81:1", t("07:05"), t("10:00"),
        "救援通道临时占用，社会车辆禁行", event_id="close:918")
    # T1/T2 已发车不动；T3 尚未发车 → 改道 BUS99；通知下游站点
    for stop, hm, channel in [
        ("滨河路口", "07:07", "signage"),
        ("迎泽桥西", "07:09", "radio"),
        ("医疗点", "07:09", "radio"),
        ("终点集散区", "07:12", "signage"),
    ]:
        d.record_notification_receipt("notice:R-918", stop, t(hm), channel,
                                      event_id=f"recv:918:{stop}")

    # 赛后接驳段无预警封闭且无替代线路 → T4 取消，站点回执缺一（太原南站）
    d.impose_closure("R-921", "SHUTTLE2:0", t("10:00"), t("12:00"),
                     "终点集散区临时人车分流", event_id="close:921")
    d.record_notification_receipt("notice:R-921", "终点集散区", t("10:03"),
                                  "signage", event_id="recv:921:zone")
    d.record_notification_receipt("notice:R-921", "地铁接驳站", t("10:04"),
                                  "radio", event_id="recv:921:metro")

    # ---- 医疗转运 + 绿色通道（不征用普通接驳，也不被管制取消） ----------
    d.request_medical_transport("M-1", "医疗点", "H1", t("07:12"),
                                severity="urgent", incident_id="INC-55",
                                event_id="med:M-1")
    d.deliver_patient("M-1", t("07:35"), event_id="done:M-1")

    # 完成的班次落库
    d.mark_completed("T1", t("07:30"), event_id="fin:T1")
    d.mark_completed("T2", t("08:05"), event_id="fin:T2")
    return svc
