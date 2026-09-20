"""权益与离线补传契约。

覆盖：时间窗按批次生效；挂失/退赛/改枪只向未来生效且历史核销依据冻结；
同一 ride_id 在线重发与离线补传重放都不重复入账；设备清单快照 + 离线
验票复算（本地误放行有 decision_match=false 标记）。
"""

import unittest

from event_transit.errors import DuplicateReplay, TransitError
from tests.util import D, build_entitlements, new_hub


class EntitlementWindowTest(unittest.TestCase):
    def setUp(self):
        self.log, self.ent, self.dis, self.tr = new_hub()
        build_entitlements(self.ent)

    def test_metro_early_window_allows(self):
        r = self.ent.validate_ride("G1", "A0001", "metro",
                                   f"{D}T05:10:00+08:00", ride_id="RD-1")
        self.assertEqual(r["verdict"]["decision"], "allow_free")

    def test_outside_window_denied(self):
        # W3 选手 05:45 刷公交：W3 公交窗 06:00 才开 → 拒
        r = self.ent.validate_ride("G1", "C0003", "bus",
                                   f"{D}T05:45:00+08:00", ride_id="RD-2")
        self.assertEqual(r["verdict"]["decision"], "deny")
        self.assertIn("outside_entitlement_window", r["verdict"]["reasons"])

    def test_wave_scoped_window(self):
        # 08:10：W1/W2 公交窗已结束（08:00），只剩 W3 窗口（至 08:30）
        w1 = self.ent.validate_ride("G1", "A0001", "bus",
                                    f"{D}T08:10:00+08:00", ride_id="RD-3")
        w3 = self.ent.validate_ride("G1", "C0003", "bus",
                                    f"{D}T08:11:00+08:00", ride_id="RD-4")
        self.assertEqual(w1["verdict"]["decision"], "deny")
        self.assertEqual(w3["verdict"]["decision"], "allow_free")

    def test_snapshot_lists_windows_with_active_flag(self):
        snap = self.ent.entitlement_snapshot("A0001", f"{D}T05:10:00+08:00")
        active = [w for w in snap["windows"] if w["active_now"]]
        self.assertEqual([w["mode"] for w in active], ["metro"])


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self.log, self.ent, self.dis, self.tr = new_hub()
        build_entitlements(self.ent)

    def test_loss_withdraw_change_wave_affect_only_future_trips(self):
        # 05:10 地铁通行正常并冻结依据
        first = self.ent.validate_ride("G1", "A0001", "metro",
                                       f"{D}T05:10:00+08:00", ride_id="RD-10")
        self.assertEqual(first["verdict"]["decision"], "allow_free")
        self.ent.report_bib_lost("A0001", occurred_at=f"{D}T07:00:00+08:00")
        self.ent.withdraw("R1", occurred_at=f"{D}T07:10:00+08:00")
        self.ent.change_wave("R1", "W2", occurred_at=f"{D}T07:20:00+08:00")
        # 09:50 再乘接驳被拒；05:10 的记录仍是 allow_free，且当时的批次是 W1
        later = self.ent.validate_ride("G1", "A0001", "shuttle",
                                       f"{D}T09:50:00+08:00", ride_id="RD-11")
        self.assertEqual(later["verdict"]["decision"], "deny")
        historical = self.log.by_type("ride_validated")[0]
        self.assertEqual(historical.data["decision"], "allow_free")
        self.assertEqual(historical.data["basis"]["wave_id"], "W1")
        self.assertTrue(historical.data["basis"]["frozen"])

    def test_replacement_bib_works_old_one_denied(self):
        self.ent.report_bib_lost("A0001", occurred_at=f"{D}T05:20:00+08:00")
        self.ent.replace_bib("A0001", "A0009", occurred_at=f"{D}T05:25:00+08:00")
        old = self.ent.validate_ride("G1", "A0001", "metro",
                                     f"{D}T05:30:00+08:00", ride_id="RD-12")
        new = self.ent.validate_ride("G1", "A0009", "metro",
                                     f"{D}T05:31:00+08:00", ride_id="RD-13")
        self.assertEqual(old["verdict"]["decision"], "deny")
        self.assertEqual(new["verdict"]["decision"], "allow_free")


class OnlineReplayTest(unittest.TestCase):
    def setUp(self):
        self.log, self.ent, self.dis, self.tr = new_hub()
        build_entitlements(self.ent)

    def test_same_ride_id_replayed_is_idempotent(self):
        kwargs = dict(at=f"{D}T05:10:00+08:00")
        self.ent.validate_ride("G1", "A0001", "metro", ride_id="RD-20", **kwargs)
        with self.assertRaises(DuplicateReplay) as cm:
            self.ent.validate_ride("G1", "A0001", "metro", ride_id="RD-20", **kwargs)
        self.assertEqual(cm.exception.details["event"]["data"]["ride_id"], "RD-20")
        self.assertEqual(len(self.log.by_type("ride_validated")), 1)


class OfflineSyncTest(unittest.TestCase):
    def setUp(self):
        self.log, self.ent, self.dis, self.tr = new_hub()
        build_entitlements(self.ent)

    def test_manifest_then_offline_records_sync_once(self):
        manifest = self.ent.device_manifest("OBU1", f"{D}T06:00:00+08:00")
        bibs = {b["bib"] for b in manifest["valid_bibs"]}
        self.assertIn("A0001", bibs)
        rec_ok = self.ent.offline_record("OFF-1", "A0001", "shuttle",
                                         f"{D}T09:40:00+08:00",
                                         "allow_free", manifest["bundle_id"])
        result = self.ent.sync_device("OBU1", [rec_ok],
                                      recorded_at=f"{D}T10:00:00+08:00")
        self.assertEqual(len(result["synced"]), 1)
        self.assertTrue(result["synced"][0]["decision_match"])

        # 整批重放：0 新增、1 重复
        again = self.ent.sync_device("OBU1", [rec_ok],
                                     recorded_at=f"{D}T10:05:00+08:00")
        self.assertEqual(again["synced"], [])
        self.assertEqual(len(again["duplicated"]), 1)
        self.assertEqual(len(self.log.by_type("ride_validated")), 1)

    def test_offline_pass_after_loss_recomputed_denied_but_kept_for_audit(self):
        manifest = self.ent.device_manifest("OBU1", f"{D}T06:00:00+08:00")
        # 设备持旧清单离线放行；随后号码布挂失；补传时按 09:45 真实状态复算
        rec = self.ent.offline_record("OFF-2", "A0001", "shuttle",
                                      f"{D}T09:45:00+08:00",
                                      "allow_free", manifest["bundle_id"])
        self.ent.report_bib_lost("A0001", occurred_at=f"{D}T07:00:00+08:00")
        result = self.ent.sync_device("OBU1", [rec],
                                      recorded_at=f"{D}T10:00:00+08:00")
        self.assertEqual(result["synced"][0]["decision"], "deny")
        self.assertFalse(result["synced"][0]["decision_match"])
        # 事件里同时保留设备本地判定与平台复算判定
        ev = self.log.by_type("ride_validated")[0]
        self.assertEqual(ev.data["local_decision"], "allow_free")
        self.assertEqual(ev.data["decision"], "deny")
        self.assertGreater(ev.recorded_at, ev.occurred_at)

    def test_change_wave_before_offline_trip_applies(self):
        manifest = self.ent.device_manifest("OBU1", f"{D}T06:00:00+08:00")
        # 06:30 把 W1 选手甲改到 W2；06:40 的公交窗判定按 W2 → WIN-B12 允许
        self.ent.change_wave("R1", "W2", occurred_at=f"{D}T06:30:00+08:00")
        rec = self.ent.offline_record("OFF-3", "A0001", "bus",
                                      f"{D}T06:40:00+08:00",
                                      "allow_free", manifest["bundle_id"])
        result = self.ent.sync_device("OBU1", [rec],
                                      recorded_at=f"{D}T07:00:00+08:00")
        self.assertEqual(result["synced"][0]["decision"], "allow_free")

    def test_malformed_record_is_quarantined_not_silently_dropped(self):
        result = self.ent.sync_device(
            "OBU1", [{"ride_id": "BAD"}], recorded_at=f"{D}T10:00:00+08:00")
        self.assertEqual(len(result["malformed"]), 1)
        self.assertEqual(result["synced"], [])