"""时间与事件日志：时区解释、去重重放、JSONL 持久化后幂等仍成立。"""

import tempfile
import unittest
from pathlib import Path

from event_transit.clock import EVENT_TZ, parse
from event_transit.errors import DuplicateReplay
from event_transit.events import EventLog


class ClockTest(unittest.TestCase):
    def test_naive_time_is_event_timezone(self):
        dt = parse("2026-09-20T05:30:00")
        self.assertEqual(dt.utcoffset().total_seconds(), 8 * 3600)
        self.assertIs(dt.tzinfo, EVENT_TZ)

    def test_zulu_converted_to_local(self):
        self.assertEqual(parse("2026-09-19T21:30:00Z"),
                         parse("2026-09-20T05:30:00+08:00"))


class EventLogTest(unittest.TestCase):
    def test_dedupe_key_applies_once(self):
        log = EventLog()
        log.append("test", {"x": 1}, dedupe_key="k1", occurred_at="2026-09-20T05:00:00+08:00")
        with self.assertRaises(DuplicateReplay) as cm:
            log.append("test", {"x": 2}, dedupe_key="k1",
                       occurred_at="2026-09-20T06:00:00+08:00")
        self.assertEqual(cm.exception.details["event"]["data"], {"x": 1})
        self.assertEqual(len(log), 1)

    def test_persistence_roundtrip_keeps_dedupe(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            log = EventLog(path)
            log.append("test", {"x": 1}, dedupe_key="k1",
                       occurred_at="2026-09-20T05:00:00+08:00")
            log2 = EventLog(path)  # 重新打开：断网补传/进程重启的模拟
            self.assertEqual(len(log2), 1)
            with self.assertRaises(DuplicateReplay):
                log2.append("test", {"x": 2}, dedupe_key="k1",
                            occurred_at="2026-09-20T05:01:00+08:00")

    def test_recorded_can_be_later_than_occurred(self):
        log = EventLog()
        e = log.append("test", {}, occurred_at="2026-09-20T09:45:00+08:00",
                       recorded_at="2026-09-20T10:00:00+08:00")
        self.assertLess(parse(e.occurred_at), parse(e.recorded_at))


if __name__ == "__main__":
    unittest.main()
