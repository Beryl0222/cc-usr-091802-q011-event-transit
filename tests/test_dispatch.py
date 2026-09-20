"""调度契约：容量原子占用与幂等、医疗资源双向隔离、绿色通道、
封控只改未发车班次、绕行重算、无绕行取消、通知回执。"""

import unittest

from event_transit.errors import Conflict, TransitError
from tests.util import D, build_entitlements, build_network, new_hub


class CapacityTest(unittest.TestCase):
    def setUp(self):
        self.log, self.ent, self.dis, self.tr = new_hub()
        build_entitlements(self.ent)
        build_network(self.dis)
        self.dis.schedule_trip("T1", "MAIN", "BUS3", f"{D}T10:00:00+08:00",
                               occurred_at=f"{D}T06:00:00+08:00")  # 容量 10

    def test_boarding_decrements_and_replay_does_not_double_count(self):
        self.dis.board_shuttle("T1", "RIDE-A", 4,
                               occurred_at=f"{D}T09:55:00+08:00")
        with self.assertRaises(Conflict) as cm:
            self.dis.board_shuttle("T1", "RIDE-A", 4,
                                   occurred_at=f"{D}T09:56:00+08:00")
        self.assertEqual(cm.exception.code, "duplicate_replay")
        status = self.dis.trip_status("T1", f"{D}T09:57:00+08:00")
        self.assertEqual(status["claimed"], 4)
        self.assertEqual(status["available"], 6)

    def test_oversell_rejected(self):
        with self.assertRaises(Conflict) as cm:
            self.dis.board_shuttle("T1", "RIDE-B", 11,
                                   occurred_at=f"{D}T09:55:00+08:00")
        self.assertEqual(cm.exception.code, "capacity_exceeded")
        status = self.dis.trip_status("T1", f"{D}T09:56:00+08:00")
        self.assertEqual(status["claimed"], 0)

    def test_boarding_cancelled_trip_rejected(self):
        # 封闭 F→A 后 MAIN 无法绕行 → T1 取消，取消车不允许登车
        self.dis.close_road("CL-X", ["S-FA"], reason="x",
                            occurred_at=f"{D}T09:00:00+08:00")
        with self.assertRaises(Conflict) as cm:
            self.dis.board_shuttle("T1", "RIDE-C", 1,
                                   occurred_at=f"{D}T09:10:00+08:00")
        self.assertEqual(cm.exception.code, "trip_cancelled")


class MedicalIsolationTest(unittest.TestCase):
    def setUp(self):
        self.log, self.ent, self.dis, self.tr = new_hub()
        build_entitlements(self.ent)
        build_network(self.dis)
        self.dis.schedule_trip("TM", "MED", "AMB1", f"{D}T08:10:00+08:00",
                               occurred_at=f"{D}T06:00:00+08:00")
        self.dis.schedule_trip("TS", "MAIN", "BUS1", f"{D}T10:00:00+08:00",
                               occurred_at=f"{D}T06:00:00+08:00")

    def test_shuttle_cannot_board_medical_vehicle(self):
        with self.assertRaises(TransitError) as cm:
            self.dis.board_shuttle("TM", "RIDE-X", 1,
                                   occurred_at=f"{D}T08:05:00+08:00")
        self.assertEqual(cm.exception.code, "medical_resource_protected")

    def test_medical_cannot_use_shuttle_vehicle(self):
        self.dis.set_green_channel("HOSP", True, occurred_at=f"{D}T07:00:00+08:00")
        with self.assertRaises(TransitError) as cm:
            self.dis.reserve_medical("TS", "CASE-9", 1, "HOSP",
                                     occurred_at=f"{D}T09:00:00+08:00")
        self.assertEqual(cm.exception.code, "shuttle_cannot_carry_medical")

    def test_green_channel_must_be_open(self):
        with self.assertRaises(Conflict) as cm:
            self.dis.reserve_medical("TM", "CASE-1", 1, "HOSP",
                                     occurred_at=f"{D}T08:00:00+08:00")
        self.assertEqual(cm.exception.code, "green_channel_closed")

    def test_reserve_then_transport_consumes_once(self):
        self.dis.set_green_channel("HOSP", True, occurred_at=f"{D}T07:00:00+08:00")
        self.dis.reserve_medical("TM", "CASE-1", 2, "HOSP",
                                 occurred_at=f"{D}T08:00:00+08:00")
        self.dis.transport_medical("TM", "CASE-1",
                                   occurred_at=f"{D}T08:11:00+08:00")
        status = self.dis.trip_status("TM", f"{D}T08:12:00+08:00")
        self.assertEqual(status["reserved_medical"], 2)
        self.assertEqual(status["available"], 2)  # 4 座，只扣一次
        self.assertEqual(status["medical_transported"], ["CASE-1"])

    def test_medical_capacity_enforced_and_replay_idempotent(self):
        self.dis.set_green_channel("HOSP", True, occurred_at=f"{D}T07:00:00+08:00")
        self.dis.reserve_medical("TM", "CASE-1", 4, "HOSP",
                                 occurred_at=f"{D}T08:00:00+08:00")
        with self.assertRaises(Conflict):
            self.dis.reserve_medical("TM", "CASE-2", 1, "HOSP",
                                     occurred_at=f"{D}T08:01:00+08:00")
        # 同病例重放不二次占位
        with self.assertRaises(Conflict) as cm:
            self.dis.reserve_medical("TM", "CASE-1", 4, "HOSP",
                                     occurred_at=f"{D}T08:02:00+08:00")
        self.assertEqual(cm.exception.code, "duplicate_replay")


class ClosureAdjustmentTest(unittest.TestCase):
    def setUp(self):
        self.log, self.ent, self.dis, self.tr = new_hub()
        build_entitlements(self.ent)
        build_network(self.dis)
        # 两辆主线车：一辆封控前已发，一辆未发
        self.dis.schedule_trip("TEARLY", "MAIN", "BUS1", f"{D}T09:40:00+08:00",
                               occurred_at=f"{D}T06:00:00+08:00")
        self.dis.schedule_trip("TLATE", "MAIN", "BUS2", f"{D}T10:10:00+08:00",
                               occurred_at=f"{D}T06:00:00+08:00")
        # 单线车不经过封控路段
        self.dis.schedule_trip("TDEAD", "DEAD", "BUS3", f"{D}T10:10:00+08:00",
                               occurred_at=f"{D}T06:00:00+08:00")
        self.dis.depart_trip("TEARLY", occurred_at=f"{D}T09:40:00+08:00")

    def test_departed_trip_untouched_late_trip_rerouted(self):
        result = self.dis.close_road("CL-1", ["S-BC"], reason="长风桥管制",
                                     occurred_at=f"{D}T09:50:00+08:00")
        actions = {a["trip_id"]: a["action"] for a in result["adjustments"]}
        self.assertNotIn("TEARLY", actions)            # 已发车不动
        self.assertEqual(actions["TLATE"], "rerouted")
        self.assertNotIn("TDEAD", actions)             # 不经过封控路段
        status = self.dis.trip_status("TLATE", f"{D}T09:55:00+08:00")
        self.assertIn("S-XY", status["path"])          # 改走支桥
        self.assertNotIn("S-BC", status["path"])
        self.assertEqual(status["status"], "scheduled")

    def test_no_alternative_path_cancels_only_unsent_trip(self):
        result = self.dis.close_road("CL-2", ["S-QR"], reason="断路",
                                     occurred_at=f"{D}T09:50:00+08:00")
        actions = {a["trip_id"]: a["action"] for a in result["adjustments"]}
        self.assertEqual(actions["TDEAD"], "cancelled")
        self.assertNotIn("TEARLY", actions)
        notifications = result["notifications"]
        self.assertTrue(notifications)
        self.assertIn("R", {n["stop_id"] for n in notifications})
        # 取消的车不允许再发车
        with self.assertRaises(Conflict) as cm:
            self.dis.depart_trip("TDEAD", occurred_at=f"{D}T10:00:00+08:00")
        self.assertEqual(cm.exception.code, "trip_cancelled")

    def test_notifications_carry_alternative_and_receipts_tracked(self):
        result = self.dis.close_road("CL-3", ["S-BC"], reason="管制",
                                     occurred_at=f"{D}T09:50:00+08:00")
        nids = [n["notification_id"] for n in result["notifications"]]
        self.assertTrue(nids)
        first = result["notifications"][0]
        self.assertIn("alternative_segments", first)
        self.dis.record_receipt(nids[0], delivered=True,
                                detail="大屏已展示",
                                occurred_at=f"{D}T09:53:00+08:00")
        # 同渠道回执重放不重复入账
        with self.assertRaises(Conflict) as cm:
            self.dis.record_receipt(nids[0], delivered=True,
                                    occurred_at=f"{D}T09:54:00+08:00")
        self.assertEqual(cm.exception.code, "duplicate_replay")
        # 未送达通知在重放中可统计：其余通知不给回执
        snap = self.tr.replay_at(f"{D}T10:00:00+08:00")
        undelivered_ids = {n["notification_id"]
                           for n in snap["undelivered_notifications"]}
        self.assertIn(nids[-1], undelivered_ids)
        self.assertNotIn(nids[0], undelivered_ids)

    def test_rerouted_trip_can_depart_on_detour(self):
        self.dis.close_road("CL-4", ["S-BC"], reason="管制",
                            occurred_at=f"{D}T09:50:00+08:00")
        self.dis.depart_trip("TLATE", occurred_at=f"{D}T10:10:00+08:00")
        status = self.dis.trip_status("TLATE", f"{D}T10:11:00+08:00")
        self.assertEqual(status["status"], "departed")

    def test_reopen_restores_segment(self):
        self.dis.close_road("CL-5", ["S-BC"], reason="管制",
                            occurred_at=f"{D}T09:50:00+08:00")
        result = self.dis.reopen_road("CL-5", occurred_at=f"{D}T11:00:00+08:00")
        self.assertEqual(result["restored_trips"], ["TLATE"])
        fleet = self.dis.fleet_snapshot(f"{D}T11:05:00+08:00")
        self.assertNotIn("S-BC", fleet["closed_segments"])
        status = self.dis.trip_status("TLATE", f"{D}T11:06:00+08:00")
        self.assertEqual(status["path"], ["S-FA", "S-AB", "S-BC", "S-CE"])

    def test_reopen_does_not_restore_departed_or_cancelled(self):
        self.dis.close_road("CL-6", ["S-BC"], reason="管制",
                            occurred_at=f"{D}T09:50:00+08:00")
        self.dis.depart_trip("TLATE", occurred_at=f"{D}T10:10:00+08:00")
        result = self.dis.reopen_road("CL-6", occurred_at=f"{D}T11:00:00+08:00")
        self.assertEqual(result["restored_trips"], [])  # 已发车不动
