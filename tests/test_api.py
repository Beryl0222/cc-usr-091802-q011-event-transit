"""HTTP 接口契约：启动真实服务（预置演示数据），用 urllib 走完整链路。"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import closing

from event_transit.api import create_server


def _request(url, method="GET", payload=None):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class ApiContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = create_server(0, load_demo=True)
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def call(self, path, method="GET", payload=None):
        return _request(f"{self.base}{path}", method, payload)

    def test_health(self):
        status, body = self.call("/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["service"], "event-transit")

    def test_entitlement_lookup_windows(self):
        status, body = self.call("/bibs/A10001/entitlements?at=2026-09-20T05:10:00%2B08:00")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["result"]["decision"], "allow")
        active = [w for w in body["result"]["windows"] if w["active_now"]]
        self.assertEqual([w["mode"] for w in active], ["metro"])

    def test_replay_and_alert_trace_endpoints(self):
        status, body = self.call("/replay?at=2026-09-20T10:00:00%2B08:00")
        self.assertEqual(status, 200)
        self.assertIn("S-BC", body["result"]["capacity"]["closed_segments"])
        self.assertTrue(body["result"]["undelivered_notifications"])

        status, body = self.call("/alerts/ALERT-77/trace")
        self.assertEqual(status, 200)
        trips = {t["trip_id"] for t in body["result"]["trips"]}
        self.assertIn("TRIP-02", trips)

    def test_ride_validation_then_replay_is_duplicate_not_double_charge(self):
        payload = {"device_id": "GATE-M01", "bib": "B20002", "mode": "metro",
                   "at": "2026-09-20T05:50:00+08:00", "ride_id": "HTTP-RD-1"}
        status, body = self.call("/rides/validate", "POST", payload)
        self.assertEqual(status, 200)
        self.assertEqual(body["result"]["verdict"]["decision"], "allow_free")
        status2, body2 = self.call("/rides/validate", "POST", payload)
        self.assertEqual(status2, 200)
        self.assertEqual(body2["status"], "duplicate")

    def test_medical_guard_and_conflict_status(self):
        # 普通接驳车 BUS-01 上做医疗预留 → 422 业务拒绝
        status, body = self.call("/medical/reserve", "POST", {
            "trip_id": "TRIP-02", "case_ref": "HTTP-CASE", "seats": 1,
            "hospital_id": "HOSP-CITY", "occurred_at": "2026-09-20T10:00:00+08:00"})
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "shuttle_cannot_carry_medical")

    def test_not_found(self):
        status, body = self.call("/bibs/NOPE/entitlements")
        self.assertEqual(status, 200)  # 未知号码布是正常判定结果而非错误
        self.assertEqual(body["result"]["decision"], "deny")
        status, body = self.call("/trips/NOPE")
        self.assertEqual(status, 404)

    def test_offline_sync_endpoint(self):
        manifest_status, manifest = self.call(
            "/devices/BUS-02-OBU/manifest?at=2026-09-20T06:00:00%2B08:00")
        self.assertEqual(manifest_status, 200)
        rec = {"ride_id": "HTTP-OFF-1", "bib": "B20002", "mode": "shuttle",
               "at": "2026-09-20T10:20:00+08:00", "local_decision": "allow_free",
               "bundle_id": manifest["result"]["bundle_id"], "online": False}
        status, body = self.call("/devices/BUS-02-OBU/sync", "POST",
                                 {"records": [rec, rec],
                                  "recorded_at": "2026-09-20T11:00:00+08:00"})
        self.assertEqual(status, 200)
        self.assertEqual(len(body["result"]["synced"]), 1)
        self.assertEqual(len(body["result"]["duplicated"]), 1)


if __name__ == "__main__":
    unittest.main()
