"""赛后追溯契约：告警因果链完整性，按原始时区重放运力/核销/未送达通知，
以及发生时间轴与登记时间轴的差异（离线补传在两条轴上出现的时刻不同）。"""

import unittest

from event_transit.demo import build as build_demo
from tests.util import D, build_entitlements, build_network, new_hub


class AlertTraceTest(unittest.TestCase):
    def setUp(self):
        result = build_demo()
        self.tr = result["trace"]
        self.dis = result["dispatch"]
        self.ent = result["entitlements"]

    def test_trace_covers_wave_runner_vehicle_segment_decision(self):
        chain = self.tr.trace_alert("ALERT-77")
        # 路段
        self.assertEqual(chain["alert"]["segment_id"], "S-BC")
        # 封控
        closure_ids = {c["closure_id"] for c in chain["closures"]}
        self.assertIn("CLOSE-01", closure_ids)
        # 班次 + 车辆 + 绕行路径 + 处置
        trips = {t["trip_id"]: t for t in chain["trips"]}
        self.assertIn("TRIP-02", trips)
        self.assertEqual(trips["TRIP-02"]["vehicle_id"], "BUS-02")
        self.assertTrue(any(a["action"] == "rerouted"
                            for a in trips["TRIP-02"]["adjustments"]))
        decisions = " ".join(d["decision"] for d in chain["decisions"])
        self.assertIn("支桥", decisions)
        # 通知与回执
        self.assertTrue(chain["notifications"])
        stops = {n["stop_id"] for n in chain["notifications"]}
        self.assertEqual(stops, {"B", "C"})
        receipted = [n for n in chain["notifications"] if n["delivered"]]
        self.assertEqual(len(receipted), 1)  # 仅 B 站送达
        # 时间线有序且至少包含告警/封控/改派/决定/通知/回执六类
        kinds = {item["kind"] for item in chain["timeline"]}
        self.assertIn("alert", kinds)
        self.assertIn("decision", kinds)
        self.assertIn("receipt", kinds)
        ats = [item["at"] for item in chain["timeline"]]
        self.assertEqual(ats, sorted(ats))

    def test_trace_links_runner_wave_via_boarding_validation(self):
        chain = self.tr.trace_alert("ALERT-77")
        trip = next(t for t in chain["trips"] if t["trip_id"] == "TRIP-02")
        # RIDE-5001（李四 B20002 / W2）在 TRIP-02 登车 → 链回选手与批次
        rides = {o["ride_id"]: o for o in trip["onboard_validations"]}
        self.assertIn("RIDE-5001", rides)
        self.assertEqual(rides["RIDE-5001"]["bib"], "B20002")
        self.assertEqual(rides["RIDE-5001"]["wave_id"], "W2")
        regs = {r["registration_id"] for r in chain["runners"]}
        self.assertIn("REG-0002", regs)
        waves = {w["wave_id"] for w in chain["waves"]}
        self.assertIn("W2", waves)


class ReplayTest(unittest.TestCase):
    def setUp(self):
        result = build_demo()
        self.tr = result["trace"]
        self.D = "2026-09-20"

    def test_replay_capacity_closure_and_notifications(self):
        before = self.tr.replay_at(f"{self.D}T09:49:00+08:00")
        self.assertNotIn("S-BC", before["capacity"]["closed_segments"])
        # 封控前 TRIP-02 仍走原线
        t2 = next(t for t in before["capacity"]["trips"] if t["trip_id"] == "TRIP-02")
        self.assertIn("S-BC", t2["path"])

        after = self.tr.replay_at(f"{self.D}T10:00:00+08:00")
        self.assertIn("S-BC", after["capacity"]["closed_segments"])
        t2 = next(t for t in after["capacity"]["trips"] if t["trip_id"] == "TRIP-02")
        self.assertIn("S-XY", t2["path"])
        self.assertNotIn("S-BC", t2["path"])
        # 一条未送达通知（C 站回执缺失）
        undelivered = after["undelivered_notifications"]
        self.assertEqual({n["stop_id"] for n in undelivered}, {"C"})

    def test_replay_windows_free_ride_counts(self):
        at0630 = self.tr.replay_at(f"{self.D}T06:30:00+08:00")
        self.assertGreaterEqual(at0630["ride_redemptions"]["free_rides"], 2)
        denied = [r for r in at0630["ride_redemptions"]["rows"]
                  if r["decision"] == "deny"]
        self.assertTrue(denied)  # C30003 在 05:45 的窗外拒绝

    def test_offline_record_appears_on_occurred_axis_before_recorded_axis(self):
        # 离线乘车发生 09:45、补传登记 10:00：
        # 09:50 时按发生轴已能看到，按登记轴调度员屏幕上还没有
        occ = self.tr.replay_at(f"{self.D}T09:50:00+08:00", axis="occurred")
        rec = self.tr.replay_at(f"{self.D}T09:50:00+08:00", axis="recorded")
        rides_occ = {r["ride_id"] for r in occ["ride_redemptions"]["rows"]}
        rides_rec = {r["ride_id"] for r in rec["ride_redemptions"]["rows"]}
        self.assertIn("RIDE-2001", rides_occ)
        self.assertNotIn("RIDE-2001", rides_rec)
        # 补传之后两条轴一致
        rec2 = self.tr.replay_at(f"{self.D}T10:30:00+08:00", axis="recorded")
        self.assertIn("RIDE-2001",
                      {r["ride_id"] for r in rec2["ride_redemptions"]["rows"]})

    def test_replay_points_in_timezone_order(self):
        series = self.tr.replay_timeline(
            [f"{self.D}T06:00:00+08:00", f"{self.D}T10:00:00+08:00",
             f"{self.D}T08:00:00+08:00"])
        self.assertEqual([p["at"] for p in series],
                         [f"{self.D}T06:00:00+08:00",
                          f"{self.D}T08:00:00+08:00",
                          f"{self.D}T10:00:00+08:00"])
        self.assertTrue(all(p["tz"] == "Asia/Shanghai" for p in series))


class GreenChannelReplayTest(unittest.TestCase):
    def test_fleet_snapshot_separates_medical_and_shuttle(self):
        log, ent, dis, tr = new_hub()
        build_entitlements(ent)
        build_network(dis)
        dis.schedule_trip("TS", "MAIN", "BUS1", f"{D}T10:00:00+08:00",
                          occurred_at=f"{D}T06:00:00+08:00")
        dis.schedule_trip("TM", "MED", "AMB1", f"{D}T10:00:00+08:00",
                          occurred_at=f"{D}T06:00:00+08:00")
        dis.set_green_channel("HOSP", True, occurred_at=f"{D}T07:00:00+08:00")
        snap = dis.fleet_snapshot(f"{D}T09:00:00+08:00")
        self.assertEqual(snap["shuttle"]["total_capacity"], 40)
        self.assertEqual(snap["medical"]["total_capacity"], 4)
        self.assertEqual(snap["green_hospitals"], ["HOSP"])
        self.assertTrue(all(t["kind"] == "medical" for t in snap["medical"]["trips"]))
