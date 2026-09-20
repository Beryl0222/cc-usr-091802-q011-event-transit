"""测试共用的领域世界构造（全部为虚构数据，时间均为 Asia/Shanghai）。"""

from event_transit.dispatch import DispatchService
from event_transit.entitlements import EntitlementService
from event_transit.events import EventLog
from event_transit.trace import TraceService

D = "2026-09-20"
T0 = f"{D}T04:00:00+08:00"


def new_hub():
    log = EventLog()
    return log, EntitlementService(log), DispatchService(log), TraceService(log)


def build_entitlements(ent: EntitlementService):
    """四枪批次 + 地铁/分批次公交/赛后接驳窗口 + 若干选手与闸机。"""
    ent.configure_policy(freeze_minutes=15, occurred_at=T0)
    ent.define_wave("W1", "第一枪", f"{D}T07:30:00+08:00", occurred_at=T0)
    ent.define_wave("W2", "第二枪", f"{D}T07:45:00+08:00", occurred_at=T0)
    ent.define_wave("W3", "第三枪", f"{D}T08:00:00+08:00", occurred_at=T0)
    ent.define_window("WIN-M", "metro", f"{D}T05:00:00+08:00",
                      f"{D}T09:30:00+08:00", occurred_at=T0)
    ent.define_window("WIN-B12", "bus", f"{D}T05:30:00+08:00",
                      f"{D}T08:00:00+08:00", waves=["W1", "W2"], occurred_at=T0)
    ent.define_window("WIN-B34", "bus", f"{D}T06:00:00+08:00",
                      f"{D}T08:30:00+08:00", waves=["W3"], occurred_at=T0)
    ent.define_window("WIN-S", "shuttle", f"{D}T09:30:00+08:00",
                      f"{D}T13:00:00+08:00", occurred_at=T0)
    ent.register_device("G1", "metro_gate", "测试闸机", occurred_at=T0)
    ent.register_device("OBU1", "onboard_unit", "测试车载设备", occurred_at=T0)
    ent.register_runner("R1", "选手甲", "W1", occurred_at=f"{D}T04:30:00+08:00")
    ent.issue_bib("A0001", "R1", occurred_at=f"{D}T04:31:00+08:00")
    ent.register_runner("R3", "选手丙", "W3", occurred_at=f"{D}T04:30:00+08:00")
    ent.issue_bib("C0003", "R3", occurred_at=f"{D}T04:31:00+08:00")


def build_network(dis: DispatchService):
    """F-A-B-C-E 主线 + B-X-Y-C 绕行支桥；另有无绕行的单线 P-Q-R。"""
    for sid, name in [("F", "起点"), ("A", "甲站"), ("B", "乙站"),
                      ("C", "丙站"), ("E", "终点"),
                      ("X", "支桥北"), ("Y", "支桥南"),
                      ("P", "P站"), ("Q", "Q站"), ("R", "R站"),
                      ("H", "医院站")]:
        dis.define_stop(sid, name, occurred_at=T0)
    for seg in [("S-FA", "F", "A"), ("S-AB", "A", "B"), ("S-BC", "B", "C"),
                ("S-CE", "C", "E"), ("S-BX", "B", "X"), ("S-XY", "X", "Y"),
                ("S-YC", "Y", "C"), ("S-PQ", "P", "Q"), ("S-QR", "Q", "R"),
                ("S-AH", "A", "H")]:
        dis.define_segment(seg[0], seg[1], seg[2], seg[0], occurred_at=T0)
    dis.define_route("MAIN", "主线接驳", ["F", "A", "B", "C", "E"],
                     ["S-FA", "S-AB", "S-BC", "S-CE"], kind="shuttle",
                     occurred_at=T0)
    dis.define_route("DEAD", "单线", ["P", "Q", "R"],
                     ["S-PQ", "S-QR"], kind="shuttle", occurred_at=T0)
    dis.define_route("MED", "医疗线", ["A", "H"], ["S-AH"],
                     kind="medical", occurred_at=T0)
    dis.define_vehicle("BUS1", "测A1", 40, "shuttle", occurred_at=T0)
    dis.define_vehicle("BUS2", "测A2", 40, "shuttle", occurred_at=T0)
    dis.define_vehicle("BUS3", "测A3", 10, "shuttle", occurred_at=T0)
    dis.define_vehicle("AMB1", "测M1", 4, "medical", occurred_at=T0)
    dis.define_hospital("HOSP", "测试医院", "H", occurred_at=T0)
