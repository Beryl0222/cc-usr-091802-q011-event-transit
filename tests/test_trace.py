"""拥堵告警链路追溯与按时刻重放。"""

import unittest

from transit.service import build_demo


class DemoTraceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.svc = build_demo()

    def test_alert_chain_wave_vehicle_segment_decision(self):
        tr = self.svc.trace("cong-BINHE")
        # 告警本身
        self.assertEqual(tr["alert"]["kind"], "congestion")
        # 车辆/班次：T2 → V-BUS-2
        self.assertEqual(tr["vehicle"]["vehicle_id"], "V-BUS-2")
        self.assertEqual(tr["scope"]["trip"]["trip_id"], "T2")
        # 批次→选手：A 枪选手 A0001 在 06:50 登乘
        waves = {w["wave_id"]: w for w in tr["wave_lineage"]}
        self.assertIn("A", waves)
        self.assertTrue(any(m["bib"] == "A0001" for m in waves["A"]["members"]))
        # 路段：R-918 封闭，替代线路 BUS99
        closure = tr["segment_lineage"]["closures"][0]
        self.assertEqual(closure["segment_id"], "BUS81:1")
        self.assertEqual(closure["alt_route_id"], "BUS99")
        # 处置：T3 改道（T1/T2 已发车不动）
        diverted = {d["trip_id"] for d in tr["decisions"]
                    if d["type"] == "TripDiverted"}
        self.assertEqual(diverted, {"T3"})
        # 通知与回执
        notice = tr["notifications"][0]
        self.assertEqual(notice["delivered_stops"],
                         ["滨河路口", "迎泽桥西", "医疗点", "终点集散区"])
        self.assertEqual(notice["pending_stops"], [])
        # 医疗：同站点伤员转运与绿色通道进入链路
        self.assertTrue(any(m["request"]["request_id"] == "M-1"
                            for m in tr["medical"]))
        # 时间线按原始时刻升序，且每条带中文摘要
        ats = [x["at"] for x in tr["timeline"]]
        self.assertEqual(ats, sorted(ats))
        self.assertTrue(all(x["summary"] for x in tr["timeline"]))

    def test_trace_unknown_alert(self):
        with self.assertRaises(LookupError):
            self.svc.trace("nope")


class ReplayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.svc = build_demo()

    def test_capacity_replay_before_during_after_closure(self):
        before = {r["trip_id"]: r
                  for r in self.svc.replay_capacity("2026-09-20T06:30:00+08:00")}
        during = {r["trip_id"]: r
                  for r in self.svc.replay_capacity("2026-09-20T07:10:00+08:00")}
        # 封闭前 T3 仍走原线
        self.assertEqual(before["T3"]["route_id"], "BUS81")
        self.assertEqual(before["T3"]["status"], "scheduled")
        # 封闭后改道
        self.assertEqual(during["T3"]["route_id"], "BUS99")
        self.assertEqual(during["T3"]["status"], "diverted")

    def test_redemption_replay_shows_basis_and_timezone(self):
        rides = self.svc.replay_redemptions("2026-09-20T08:00:00+08:00")
        off = [r for r in rides if r["event_id"] == "off:A0002:1"]
        self.assertEqual(len(off), 1)  # 离线重放只核销一次
        self.assertEqual(off[0]["basis"], "offline-snapshot")
        self.assertTrue(off[0]["at"].startswith("2026-09-20T05:10:00+08:00"))
        self.assertNotEqual(off[0]["at"], off[0]["recorded_at"])  # 双时间

    def test_undelivered_replay_only_pending(self):
        at = "2026-09-20T11:00:00+08:00"
        pending = {n["restriction_id"]: n
                   for n in self.svc.replay_undelivered(at)}
        self.assertNotIn("R-918", pending)       # 全部回执
        self.assertEqual(pending["R-921"]["pending_stops"], ["太原南站"])
        # 未补回执前，更晚时刻重放仍为未送达
        later = {n["restriction_id"]: n
                 for n in self.svc.replay_undelivered("2026-09-20T12:30:00+08:00")}
        self.assertEqual(later["R-921"]["pending_stops"], ["太原南站"])

    def test_alerts_listed(self):
        alerts = {a["alert_id"]: a for a in self.svc.alerts()}
        self.assertIn("cong-BINHE", alerts)
        self.assertIn("cap:T1", alerts)


if __name__ == "__main__":
    unittest.main()
