"""权益域：凭证窗口、批次、挂失/退赛/改枪、离线验票补传。"""

import unittest

from transit.entitlements import EntitlementDomain, list_redemptions
from transit.events import EventStore


def seed(store=None):
    store = store or EventStore()
    e = EntitlementDomain(store)
    e.configure_wave("A", "第一枪", "2026-09-20T07:00:00+08:00",
                     configured_at="2026-09-18T09:00:00+08:00",
                     wave_idem="w:A")
    e.configure_wave("B", "第二枪", "2026-09-20T07:15:00+08:00",
                     configured_at="2026-09-18T09:00:00+08:00",
                     wave_idem="w:B")
    e.register_runner("R1", "A", "2026-09-18T10:00:00+08:00", event_id="r:R1")
    e.register_runner("R2", "A", "2026-09-18T10:00:00+08:00", event_id="r:R2")
    e.issue_bib("R1", "A001", "2026-09-19T15:00:00+08:00", event_id="b:A001")
    e.issue_bib("R2", "A002", "2026-09-19T15:00:00+08:00", event_id="b:A002")
    e.open_window({"window_id": "metro", "mode": "metro",
                   "title": "地铁提前运营",
                   "start_at": "2026-09-20T04:30:00+08:00",
                   "end_at": "2026-09-20T09:30:00+08:00"},
                  at="2026-09-20T04:00:00+08:00", event_id="win:metro")
    e.open_window({"window_id": "post", "mode": "shuttle",
                   "title": "赛后接驳",
                   "start_at": "2026-09-20T09:30:00+08:00",
                   "end_at": "2026-09-20T15:00:00+08:00",
                   "waves": ["B"]},
                  at="2026-09-20T04:00:00+08:00", event_id="win:post")
    return e


class WindowEligibilityTest(unittest.TestCase):
    def setUp(self):
        self.e = seed()

    def test_valid_redeem_inside_window(self):
        r = self.e.redeem("A001", "metro", "2026-09-20T05:30:00+08:00",
                          redeem_id="ride-1")
        self.assertTrue(r.accepted)
        self.assertEqual(r.basis, "online")

    def test_reject_before_open_and_after_close(self):
        early = self.e.redeem("A001", "metro", "2026-09-20T04:29:00+08:00",
                              redeem_id="ride-early")
        late = self.e.redeem("A001", "metro", "2026-09-20T09:31:00+08:00",
                             redeem_id="ride-late")
        self.assertEqual(early.reason, "outside_window")
        self.assertEqual(late.reason, "outside_window")

    def test_reject_unknown_bib_and_wave_not_eligible(self):
        ghost = self.e.redeem("X999", "metro", "2026-09-20T05:00:00+08:00",
                              redeem_id="ride-x")
        # A 枪选手不能用仅对 B 枪开放的赛后窗口
        wrong = self.e.redeem("A001", "post", "2026-09-20T10:00:00+08:00",
                              redeem_id="ride-w")
        self.assertEqual(ghost.reason, "unknown_bib")
        self.assertEqual(wrong.reason, "wave_not_eligible")


class TemporalChangeTest(unittest.TestCase):
    def setUp(self):
        self.e = seed()

    def test_lost_bib_only_affects_later_rides(self):
        # 06:00 完成的通行有效
        before = self.e.redeem("A001", "metro", "2026-09-20T06:00:00+08:00",
                               redeem_id="rb")
        self.e.report_lost("A001", "2026-09-20T07:00:00+08:00", event_id="lost")
        after = self.e.redeem("A001", "metro", "2026-09-20T07:10:00+08:00",
                              redeem_id="ra")
        self.assertTrue(before.accepted)
        self.assertEqual(after.reason, "bib_lost")
        # 补领后恢复
        self.e.issue_replacement("R1", "A101", "2026-09-20T07:30:00+08:00",
                                 event_id="repl")
        replaced = self.e.redeem("A101", "post", "2026-09-20T10:00:00+08:00",
                                 redeem_id="rr")
        # R1 属 A 枪，post 仅 B 枪可用——证明权益随人转移而非新身份
        self.assertEqual(replaced.reason, "wave_not_eligible")

    def test_withdraw_keeps_completed_rides(self):
        done = self.e.redeem("A002", "metro", "2026-09-20T05:40:00+08:00",
                             redeem_id="done")
        self.e.withdraw("R2", "2026-09-20T08:00:00+08:00", event_id="wd")
        denied = self.e.redeem("A002", "post", "2026-09-20T10:00:00+08:00",
                               redeem_id="denied")
        self.assertTrue(done.accepted)
        self.assertEqual(denied.reason, "withdrawn")

    def test_wave_change_only_moves_later_trips(self):
        # R1: A→B 于 06:30 生效
        self.e.change_wave("R1", "B", "2026-09-20T06:30:00+08:00",
                           event_id="chg")
        # 06:00 的地铁核销发生时仍是 A（窗口不限批次，均放行）
        self.e.redeem("A001", "metro", "2026-09-20T06:00:00+08:00",
                      redeem_id="r-before")
        self.e.redeem("A001", "metro", "2026-09-20T06:40:00+08:00",
                      redeem_id="r-after")
        rides = {r["at"][11:16]: r["wave_id"]
                 for r in list_redemptions(self.e.store)
                 if r["bib"] == "A001"}
        self.assertEqual(rides["06:00"], "A")
        self.assertEqual(rides["06:40"], "B")


class OfflineScanTest(unittest.TestCase):
    def setUp(self):
        self.e = seed()

    def test_snapshot_verify_upload_and_replay_dedup(self):
        snap = self.e.issue_snapshot("GATE-9", "2026-09-20T04:25:00+08:00",
                                     event_id="snap")
        self.assertTrue(any(b["bib"] == "A001" for b in snap["active_bibs"]))
        scans = [
            {"redeem_id": "off-1", "bib": "A001", "window_id": "metro",
             "scanned_at": "2026-09-20T05:10:00+08:00", "device_result": "accepted"},
            {"redeem_id": "off-1", "bib": "A001", "window_id": "metro",
             "scanned_at": "2026-09-20T05:10:00+08:00", "device_result": "accepted"},
        ]
        results = self.e.upload_offline_scans("GATE-9", scans)
        self.assertEqual(len(results), 2)
        self.assertTrue(results[0].accepted)
        self.assertIn("duplicate_replay_ignored", results[1].warnings)
        # 事件日志中只有一条
        rides = [r for r in list_redemptions(self.e.store)
                 if r["event_id"] == "off-1"]
        self.assertEqual(len(rides), 1)

    def test_offline_accepted_after_loss_is_kept_but_flagged(self):
        self.e.issue_snapshot("GATE-9", "2026-09-20T04:25:00+08:00",
                              event_id="snap")
        self.e.report_lost("A001", "2026-09-20T06:00:00+08:00", event_id="lost")
        results = self.e.upload_offline_scans("GATE-9", [
            {"redeem_id": "off-lost", "bib": "A001", "window_id": "metro",
             "scanned_at": "2026-09-20T06:30:00+08:00",
             "device_result": "accepted", "snapshot_id": "snap"}])
        self.assertTrue(results[0].accepted)  # 已完成通行不翻案
        self.assertTrue(
            any(w.startswith("offline_basis_mismatch:bib_lost")
                for w in results[0].warnings))


if __name__ == "__main__":
    unittest.main()
