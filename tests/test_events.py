"""事件日志幂等性、双时间、持久化与按时区重放。"""

import json
import tempfile
import unittest
from pathlib import Path

from transit.clock import EVENT_TIMEZONE, parse_event_time
from transit.events import EventStore


class EventStoreTest(unittest.TestCase):
    def test_idempotent_append_returns_existing(self):
        store = EventStore()
        e1, created1 = store.append("X", {"v": 1}, "2026-09-20T05:00:00+08:00",
                                    event_id="idem-1")
        e2, created2 = store.append("X", {"v": 2}, "2026-09-20T06:00:00+08:00",
                                    event_id="idem-1")
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertIs(e1, e2)
        self.assertEqual(e1.payload["v"], 1)  # 重放不覆盖
        self.assertEqual(len(store.all()), 1)

    def test_offline_late_upload_sorts_by_event_time(self):
        store = EventStore()
        store.append("Online", {}, "2026-09-20T08:00:00+08:00", event_id="on")
        # 09:00 才补传 05:30 的断网记录
        store.append("Offline", {}, "2026-09-20T05:30:00+08:00",
                     event_id="off", recorded_at="2026-09-20T09:00:00+08:00")
        events = store.replay()
        self.assertEqual([e.event_type for e in events], ["Offline", "Online"])
        by_record = store.replay(order="recorded")
        self.assertEqual([e.event_type for e in by_record], ["Online", "Offline"])

    def test_as_of_cutoff_and_type_filter(self):
        store = EventStore()
        store.append("A", {}, "2026-09-20T05:00:00+08:00", event_id="a")
        store.append("B", {}, "2026-09-20T09:00:00+08:00", event_id="b")
        self.assertEqual(
            [e.event_id for e in store.replay("2026-09-20T06:00:00+08:00")], ["a"])
        self.assertEqual(
            [e.event_id for e in store.replay(types=["B"])], ["b"])

    def test_jsonl_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "log.jsonl"
            store = EventStore(path)
            store.append("X", {"k": "值"}, "2026-09-20T05:00:00+08:00",
                          event_id="x1")
            reloaded = EventStore(path)
            self.assertEqual(len(reloaded.all()), 1)
            self.assertEqual(reloaded.get("x1").payload["k"], "值")
            # 再追加不重复加载
            reloaded.append("Y", {}, "2026-09-20T06:00:00+08:00", event_id="y1")
            self.assertEqual(len(EventStore(path).all()), 2)
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertTrue(all(json.loads(line) for line in lines))

    def test_timezone_interpretation(self):
        naive = parse_event_time("2026-09-20T05:00:00")
        self.assertEqual(naive.utcoffset().total_seconds(), 8 * 3600)
        zulu = parse_event_time("2026-09-20T01:00:00Z")
        self.assertEqual(zulu.astimezone(EVENT_TIMEZONE).hour, 9)


if __name__ == "__main__":
    unittest.main()
