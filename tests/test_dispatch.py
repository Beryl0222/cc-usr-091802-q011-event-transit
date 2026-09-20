"""调度域：容量占用幂等、医疗隔离、封闭改派边界、通知回执与绿色通道。"""

import unittest

from transit.dispatch import (
    DispatchDomain, build_dispatch_state, capacity_snapshot,
    undelivered_notifications,
)
from transit.events import EventStore

DAY = "2026-09-20"


def _at(hm):
    return f"{DAY}T{hm}:00+08:00"


def seed_network(store=None):
    store = store or EventStore()
    d = DispatchDomain(store)
    d.register_route("R81", "保障线", "bus",
                     ["S0", "S1", "S2", "S3"], _at("03:00"), event_id="rt:81")
    d.register_route("R99", "绕行线", "bus",
                     ["S0", "N1", "N2", "S3"], _at("03:00"), event_id="rt:99")
    d.register_alternative("R81:1", "R99", "经北中环绕行", _at("03:00"),
                           event_id="alt:1")
    d.register_route("R2", "赛后线", "shuttle",
                     ["S3", "M1", "M2"], _at("03:00"), event_id="rt:2")
    d.register_vehicle("V1", 2, _at("03:00"), event_id="v:1")
    d.register_vehicle("V2", 40, _at("03:00"), event_id="v:2")
    d.register_vehicle("V3", 40, _at("03:00"), event_id="v:3")
    d.register_vehicle("MED", 2, _at("03:00"), medical=True, event_id="v:med")
    d.register_hospital("H1", "市一院", _at("03:00"), event_id="h:1")
    d.schedule_trip("T-departed", "R81", "V2", _at("06:30"),
                    scheduled_at=_at("03:00"), event_id="t:dep")
    d.schedule_trip("T-future", "R81", "V3", _at("07:30"),
                    scheduled_at=_at("03:00"), event_id="t:fut")
    d.schedule_trip("T-shuttle", "R2", "V1", _at("10:30"),
                    scheduled_at=_at("03:00"), event_id="t:shu")
    d.schedule_trip("T-med", "R81", "MED", _at("07:00"),
                    scheduled_at=_at("03:00"), event_id="t:med")
    return d


class CapacityTest(unittest.TestCase):
    def setUp(self):
        self.d = seed_network()

    def test_boarding_occupies_once_and_full_rejects(self):
        r1 = self.d.board("T-shuttle", "A1", _at("10:00"), "seat-1")
        r1_again = self.d.board("T-shuttle", "A1", _at("10:01"), "seat-1")
        r2 = self.d.board("T-shuttle", "A2", _at("10:02"), "seat-2")
        r3 = self.d.board("T-shuttle", "A3", _at("10:03"), "seat-3")
        self.assertTrue(r1.accepted)
        self.assertTrue(r1_again.accepted)
        self.assertEqual(r1.event_id, r1_again.event_id)
        self.assertTrue(r2.accepted)
        self.assertFalse(r3.accepted)
        self.assertEqual(r3.reason, "capacity_full")
        rows = {r["trip_id"]: r for r in capacity_snapshot(self.d.store)}
        self.assertEqual(rows["T-shuttle"]["occupied"], 2)
        self.assertEqual(rows["T-shuttle"]["available"], 0)
        # 满员产生且仅产生一条告警
        alerts = [a for a in self.d.store.all() if a.event_type == "AlertRaised"]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].payload["alert_id"], "cap:T-shuttle")

    def test_regular_passenger_cannot_use_medical_vehicle(self):
        r = self.d.board("T-med", "A9", _at("06:50"), "seat-med-1")
        self.assertFalse(r.accepted)
        self.assertEqual(r.reason, "medical_resource_reserved")
        rows = {x["trip_id"]: x for x in capacity_snapshot(self.d.store)}
        self.assertEqual(rows["T-med"]["occupied"], 0)

    def test_no_boarding_after_departure(self):
        self.d.mark_departed("T-departed", _at("06:30"), event_id="dep")
        r = self.d.board("T-departed", "A7", _at("06:31"), "late-seat")
        self.assertFalse(r.accepted)
        self.assertEqual(r.reason, "trip_departed")


class ClosureTest(unittest.TestCase):
    def setUp(self):
        self.d = seed_network()
        # 已发车的班次在封闭前发车
        self.d.mark_departed("T-departed", _at("06:30"), event_id="dep")

    def test_closure_only_touches_undeparted_trips(self):
        self.d.impose_closure("R-1", "R81:1", _at("06:45"), _at("09:00"),
                              "交通管制", event_id="cl:1")
        rows = {r["trip_id"]: r for r in capacity_snapshot(self.d.store)}
        # 已发车：保留原线路原状态
        self.assertEqual(rows["T-departed"]["status"], "departed")
        self.assertEqual(rows["T-departed"]["route_id"], "R81")
        # 未发车：改道至替代线路
        self.assertEqual(rows["T-future"]["status"], "diverted")
        self.assertEqual(rows["T-future"]["route_id"], "R99")
        # 医疗班不被普通改派/取消波及
        self.assertEqual(rows["T-med"]["status"], "scheduled")
        self.assertEqual(rows["T-med"]["route_id"], "R81")

    def test_closure_is_idempotent(self):
        self.d.impose_closure("R-1", "R81:1", _at("06:45"), _at("09:00"),
                              "交通管制", event_id="cl:1")
        self.d.impose_closure("R-1", "R81:1", _at("06:45"), _at("09:00"),
                              "交通管制", event_id="cl:1")
        divert = [e for e in self.d.store.all() if e.event_type == "TripDiverted"]
        notices = [e for e in self.d.store.all()
                   if e.event_type == "SiteNotificationIssued"]
        self.assertEqual(len(divert), 1)
        self.assertEqual(len(notices), 1)

    def test_notification_receipts_and_undelivered(self):
        self.d.impose_closure("R-1", "R81:1", _at("06:45"), _at("09:00"),
                              "交通管制", event_id="cl:1")
        # 只给部分站点回执
        self.d.record_notification_receipt("notice:R-1", "S1", _at("06:47"),
                                           event_id="rc:S1")
        pending = undelivered_notifications(self.d.store)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["pending_stops"], ["S2", "S3"])
        # 补回执后清空
        self.d.record_notification_receipt("notice:R-1", "S2", _at("06:48"),
                                           event_id="rc:S2")
        self.d.record_notification_receipt("notice:R-1", "S3", _at("06:49"),
                                           event_id="rc:S3")
        self.assertEqual(undelivered_notifications(self.d.store), [])

    def test_cancel_when_no_alternative(self):
        # R2:0 封闭，未登记替代线路 → T-shuttle 取消
        self.d.impose_closure("R-2", "R2:0", _at("09:50"), _at("12:00"),
                              "人车分流", event_id="cl:2")
        rows = {r["trip_id"]: r for r in capacity_snapshot(self.d.store)}
        self.assertEqual(rows["T-shuttle"]["status"], "cancelled")
        pending = undelivered_notifications(self.d.store)
        self.assertEqual(len(pending), 1)
        self.assertIn("暂无替代线路", pending[0]["message"])

    def test_diverted_trip_still_boardable_on_alt_route(self):
        self.d.impose_closure("R-1", "R81:1", _at("06:45"), _at("09:00"),
                              "交通管制", event_id="cl:1")
        r = self.d.board("T-future", "A5", _at("07:00"), "div-seat")
        self.assertTrue(r.accepted)


class MedicalTest(unittest.TestCase):
    def setUp(self):
        self.d = seed_network()

    def test_green_channel_and_delivery(self):
        out = self.d.request_medical_transport(
            "M-9", "S2", "H1", _at("07:10"), event_id="m:9")
        self.assertEqual(out["vehicle_id"], "MED")
        self.d.deliver_patient("M-9", _at("07:40"), event_id="done:9")
        state = build_dispatch_state(self.d.store)
        self.assertTrue(state["medical"]["M-9"]["delivered"])
        green = state["hospitals"]["H1"]["green_channels"][0]
        self.assertEqual(green["request_id"], "M-9")

    def test_medical_pool_does_not_take_regular_vehicles(self):
        # 占用唯一医疗车后再次请求：无车可派但请求与绿色通道仍留痕
        self.d.request_medical_transport("M-a", "S1", "H1", _at("07:10"),
                                         event_id="m:a")
        second = self.d.request_medical_transport("M-b", "S2", "H1", _at("07:12"),
                                                  event_id="m:b")
        self.assertIsNone(second["vehicle_id"])


if __name__ == "__main__":
    unittest.main()
