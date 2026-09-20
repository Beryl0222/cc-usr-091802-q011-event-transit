"""太原马拉松比赛日演示场景。

用只增事件还原一条完整故事线（全部为虚构数据）：

  四枪发令安排 → 地铁提前运营/公交凭号码布免费/赛后接驳窗口
  → 选手报名与号码布 → 设备断网验票 → 补传（含重放）
  → 选手挂失（旧通行保留）→ 接驳车容量与超售拒绝
  → 医疗车专用运力与普通接驳隔离 → 突发封控只改未发车班次
  → 站点通知与回执（含一条未送达）→ 拥堵告警 → 处置决定
  → 赛后告警溯源 + 按原始时区多点重放
"""

from __future__ import annotations

from .clock import format_dt
from .dispatch import DispatchService
from .entitlements import EntitlementService
from .events import EventLog
from .trace import TraceService

D = "2026-09-20"  # 比赛日，时区一律 Asia/Shanghai


def build(log: EventLog | None = None, *, verbose: bool = False) -> dict:
    # 注意：空日志 bool 为假，必须显式判 None，否则传入的日志会被新日志替换
    log = log if log is not None else EventLog()
    ent = EntitlementService(log)
    dis = DispatchService(log)
    trace = TraceService(log)

    def say(msg):
        if verbose:
            print(msg)

    # ------------------------------------------------------------ 策略与批次
    ent.configure_policy(freeze_minutes=15, medical_isolation=True,
                         occurred_at=f"{D}T00:00:00+08:00")
    waves = [
        ("W1", "第一枪 全程马拉松", f"{D}T07:30:00+08:00", "red"),
        ("W2", "第二枪 半程马拉松", f"{D}T07:45:00+08:00", "blue"),
        ("W3", "第三枪 欢乐跑", f"{D}T08:00:00+08:00", "green"),
        ("W4", "第四枪 家庭跑", f"{D}T08:15:00+08:00", "yellow"),
    ]
    for wid, name, start, color in waves:
        ent.define_wave(wid, name, start, color=color)

    # 地铁提前运营（全员）；公交凭号码布免费（按批次窗）；赛后接驳
    ent.define_window("WIN-METRO-EARLY", "metro",
                      f"{D}T05:00:00+08:00", f"{D}T09:30:00+08:00",
                      benefit="free_ride", note="地铁提前运营，选手与陪同人员免费")
    ent.define_window("WIN-BUS-W1W2", "bus",
                      f"{D}T05:30:00+08:00", f"{D}T08:00:00+08:00",
                      waves=["W1", "W2"], note="凭号码布免费乘公交（全/半程批次）")
    ent.define_window("WIN-BUS-W3W4", "bus",
                      f"{D}T06:00:00+08:00", f"{D}T08:30:00+08:00",
                      waves=["W3", "W4"], note="凭号码布免费乘公交（欢乐/家庭批次）")
    ent.define_window("WIN-POST-SHUTTLE", "shuttle",
                      f"{D}T09:30:00+08:00", f"{D}T13:00:00+08:00",
                      note="终点 → 集散点 赛后免费接驳")

    # ------------------------------------------------------------ 路网
    # 站点：起点 F / 滨河东路站 A / 长风桥站 B / 南中环站 C / 终点 E
    #                   绕行支桥 X-Y
    SETUP = f"{D}T04:00:00+08:00"
    stops = [("F", "起点拱门站"), ("A", "滨河东路站"), ("B", "长风桥站"),
             ("C", "南中环站"), ("E", "终点集散站"),
             ("X", "支桥北站"), ("Y", "支桥南站"),
             ("H1", "太原市中心医院"), ("H2", "煤炭中心医院")]
    for sid, name in stops:
        dis.define_stop(sid, name, occurred_at=SETUP)
    segments = [
        ("S-FA", "F", "A", "起点—滨河东路"),
        ("S-AB", "A", "B", "滨河东路—长风桥"),
        ("S-BC", "B", "C", "长风桥—南中环"),
        ("S-CE", "C", "E", "南中环—终点"),
        # 平行绕行支桥 B-X-Y-C
        ("S-BX", "B", "X", "长风桥—支桥北"),
        ("S-XY", "X", "Y", "支桥"),
        ("S-YC", "Y", "C", "支桥南—南中环"),
        # 医疗专用线 A→H1，F→H2
        ("S-AH1", "A", "H1", "滨河东路—市中心医院"),
        ("S-FH2", "F", "H2", "起点—煤炭中心医院"),
    ]
    for seg_id, a, b, name in segments:
        dis.define_segment(seg_id, a, b, name, occurred_at=SETUP)
    dis.define_route("R1", "赛后接驳1号线", ["F", "A", "B", "C", "E"],
                     ["S-FA", "S-AB", "S-BC", "S-CE"], kind="shuttle",
                     occurred_at=SETUP)
    dis.define_route("RM1", "医疗转运1号线", ["A", "H1"], ["S-AH1"],
                     kind="medical", occurred_at=SETUP)
    dis.define_route("RM2", "医疗转运2号线", ["F", "H2"], ["S-FH2"],
                     kind="medical", occurred_at=SETUP)

    dis.define_vehicle("BUS-01", "晋A·10001", 40, "shuttle", occurred_at=SETUP)
    dis.define_vehicle("BUS-02", "晋A·20002", 40, "shuttle", occurred_at=SETUP)
    dis.define_vehicle("AMB-01", "晋A·M9001", 4, "medical", occurred_at=SETUP)
    dis.define_vehicle("AMB-02", "晋A·M9002", 6, "medical", occurred_at=SETUP)

    dis.define_hospital("HOSP-CITY", "太原市中心医院", "H1", occurred_at=SETUP)
    dis.define_hospital("HOSP-COAL", "煤炭中心医院", "H2", occurred_at=SETUP)
    dis.set_green_channel("HOSP-CITY", True, occurred_at=f"{D}T05:00:00+08:00")

    # ------------------------------------------------------------ 选手与号码布
    ent.register_runner("REG-0001", "张三（演示）", "W1",
                        occurred_at=f"{D}T04:30:00+08:00")
    ent.issue_bib("A10001", "REG-0001", occurred_at=f"{D}T04:35:00+08:00")
    ent.register_runner("REG-0002", "李四（演示）", "W2",
                        occurred_at=f"{D}T04:40:00+08:00")
    ent.issue_bib("B20002", "REG-0002", occurred_at=f"{D}T04:42:00+08:00")
    ent.register_runner("REG-0003", "王五（演示）", "W3",
                        occurred_at=f"{D}T04:45:00+08:00")
    ent.issue_bib("C30003", "REG-0003", occurred_at=f"{D}T04:47:00+08:00")
    # REG-0004 将在领物后改枪
    ent.register_runner("REG-0004", "赵六（演示）", "W1",
                        occurred_at=f"{D}T04:50:00+08:00")
    ent.issue_bib("A10004", "REG-0004", occurred_at=f"{D}T04:52:00+08:00")

    # ------------------------------------------------------------ 设备
    ent.register_device("GATE-M01", "metro_gate", "地铁2号线涧河站",
                        occurred_at=f"{D}T04:55:00+08:00")
    ent.register_device("BUS-01-OBU", "onboard_unit", "接驳车 BUS-01",
                        occurred_at=f"{D}T04:55:00+08:00")
    ent.register_device("BUS-02-OBU", "onboard_unit", "接驳车 BUS-02",
                        occurred_at=f"{D}T04:55:00+08:00")

    # 05:10 地铁闸机刷码（免费窗口已生效）
    r1 = ent.validate_ride("GATE-M01", "A10001", "metro",
                           f"{D}T05:10:00+08:00", ride_id="RIDE-1001")
    say(f"地铁验票 A10001 → {r1['verdict']['decision']}")

    # W2 选手在 W1/W2 公交窗前不能提前免费（05:45 < 窗起点 05:30? 05:45 在内）
    # 用一个明确的窗外案例：W3 选手 05:45 刷公交 → 拒（W3/W4 窗 06:00 才开）
    deny = ent.validate_ride("GATE-M01", "C30003", "bus",
                             f"{D}T05:45:00+08:00", ride_id="RIDE-1002")
    say(f"公交验票 C30003@05:45 → {deny['verdict']['decision']} {deny['verdict']['reasons']}")
    ok = ent.validate_ride("GATE-M01", "C30003", "bus",
                           f"{D}T06:05:00+08:00", ride_id="RIDE-1003")
    say(f"公交验票 C30003@06:05 → {ok['verdict']['decision']}")

    # ------------------------------------------------------------ 断网验票 + 补传
    # 06:20 起车载设备断网；06:25 离线放行一名选手（本地清单含其号码布）
    manifest = ent.device_manifest("BUS-01-OBU", f"{D}T06:20:00+08:00")
    offline = EntitlementService.offline_record(
        "RIDE-2001", "A10001", "shuttle", f"{D}T09:45:00+08:00",
        "allow_free", manifest["bundle_id"])
    # 06:20 时赵六仍在 W1；调度员 06:40 把赵六改到 W2（效力只向未来）
    ent.change_wave("REG-0004", "W2", occurred_at=f"{D}T06:40:00+08:00")
    # 张三 07:00 挂失号码布（此后的行程才受影响；09:45 的离线乘车将被复算拒绝）
    ent.report_bib_lost("A10001", occurred_at=f"{D}T07:00:00+08:00")

    # ------------------------------------------------------------ 班次与容量
    dis.schedule_trip("TRIP-01", "R1", "BUS-01", f"{D}T09:40:00+08:00",
                      occurred_at=f"{D}T06:00:00+08:00")
    dis.schedule_trip("TRIP-02", "R1", "BUS-02", f"{D}T10:10:00+08:00",
                      occurred_at=f"{D}T06:00:00+08:00")
    dis.schedule_trip("TRIP-M1", "RM1", "AMB-01", f"{D}T08:05:00+08:00",
                      occurred_at=f"{D}T06:00:00+08:00")
    dis.schedule_trip("TRIP-M2", "RM2", "AMB-02", f"{D}T08:20:00+08:00",
                      occurred_at=f"{D}T06:00:00+08:00")

    # 医疗病例预留 + 隔离规则
    med = dis.reserve_medical("TRIP-M1", "CASE-01", 2, "HOSP-CITY",
                              occurred_at=f"{D}T08:02:00+08:00")
    say(f"医疗预留 CASE-01 → 余 {med['medical_remaining']}")
    dis.transport_medical("TRIP-M1", "CASE-01",
                          occurred_at=f"{D}T08:06:00+08:00")

    # ------------------------------------------------------------ 突发封控：长风桥段 09:50 封闭
    # TRIP-01 09:40 准时发车——必须先于封控入账，封控时它才会被视为"已发车、不动"
    dis.depart_trip("TRIP-01", occurred_at=f"{D}T09:40:00+08:00")
    closure = dis.close_road(
        "CLOSE-01", ["S-BC"], reason="长风桥临时交通管制",
        occurred_at=f"{D}T09:50:00+08:00")
    say(f"封控调整：{[(a['trip_id'], a['action']) for a in closure['adjustments']]}")
    # TRIP-01 已发车不动；TRIP-02 10:10 未发 → 被改派走支桥 B-X-Y-C

    # 站点回执：B 站送达，C 站回执丢失（制造一条未送达通知）
    note_b = next(n["notification_id"] for n in closure["notifications"]
                  if n["stop_id"] == "B")
    dis.record_receipt(note_b, delivered=True, detail="站内大屏已展示",
                       occurred_at=f"{D}T09:52:00+08:00")

    # ------------------------------------------------------------ 网络恢复：补传
    sync = ent.sync_device("BUS-01-OBU", [offline],
                           recorded_at=f"{D}T10:00:00+08:00")
    say(f"补传结果：{sync['synced']}；挂失后离线放行被复算为 "
        f"{sync['synced'][0]['decision'] if sync['synced'] else '-'}")
    # 同一批记录重放：0 新增、1 重复，容量不变
    replay = ent.sync_device("BUS-01-OBU", [offline],
                             recorded_at=f"{D}T10:05:00+08:00")
    say(f"重放补传：synced={len(replay['synced'])} duplicated={len(replay['duplicated'])}")

    # 容量幂等：李四先验票后登车；同一登车记录重放被去重拦截，不二次占座
    ent.validate_ride("BUS-02-OBU", "B20002", "shuttle",
                      f"{D}T10:04:00+08:00", ride_id="RIDE-5001")
    try:
        dis.board_shuttle("TRIP-02", "RIDE-5001", 1,
                          occurred_at=f"{D}T10:05:00+08:00")
        dis.board_shuttle("TRIP-02", "RIDE-5001", 1,  # 重放
                          occurred_at=f"{D}T10:06:00+08:00")
    except Exception as exc:  # noqa: BLE001 - 演示中打印
        say(f"重放登车被拒绝：{getattr(exc, 'code', '')}")

    # 医疗隔离的两个方向
    guard = []
    try:
        dis.board_shuttle("TRIP-M1", "RIDE-X", 1,
                          occurred_at=f"{D}T08:04:00+08:00")
    except Exception as exc:  # noqa: BLE001
        guard.append(getattr(exc, "code", ""))
    try:
        dis.reserve_medical("TRIP-02", "CASE-02", 1, "HOSP-CITY",
                            occurred_at=f"{D}T10:00:00+08:00")
    except Exception as exc:  # noqa: BLE001
        guard.append(getattr(exc, "code", ""))
    say(f"医疗隔离拒绝码：{guard}")

    # 拥堵告警与处置
    dis.report_congestion("ALERT-77", "S-BC", "severe",
                          note="长风桥东向西拥堵超 20 分钟",
                          observed_at=f"{D}T09:48:00+08:00")
    rerouted_trips = [a["trip_id"] for a in closure["adjustments"]
                      if a["action"] == "rerouted"]
    dis.handle_alert("ALERT-77", "未发车班次改行支桥 B-X-Y-C，已发车班次现场疏导",
                     note="联动交警 CLOSE-01",
                     refs=[*(rerouted_trips[:1]), "CLOSE-01"],
                     occurred_at=f"{D}T09:55:00+08:00")

    # 让 TRIP-02 按绕行路径发车
    if rerouted_trips:
        dis.depart_trip(rerouted_trips[0], occurred_at=f"{D}T10:10:00+08:00")

    return {
        "log": log, "entitlements": ent, "dispatch": dis, "trace": trace,
        "closure": closure, "sync": sync, "replay_sync": replay,
        "guard_codes": guard,
        "alert_id": "ALERT-77",
    }


if __name__ == "__main__":
    result = build(verbose=True)
    tr = result["trace"]
    print("\n=== 10:00 时刻重放（按发生时间） ===")
    snap = tr.replay_at(f"{D}T10:00:00+08:00")
    print(f"接驳可用座位 {snap['capacity']['shuttle_available']}，"
          f"医疗可用座位 {snap['capacity']['medical_available']}，"
          f"封控路段 {snap['capacity']['closed_segments']}，"
          f"未送达通知 {len(snap['undelivered_notifications'])} 条")
    print("\n=== 告警 ALERT-77 溯源时间线 ===")
    chain = tr.trace_alert("ALERT-77")
    for item in chain["timeline"]:
        print(f"[{item['at']}] {item['kind']:>12}  {item['summary']}")
