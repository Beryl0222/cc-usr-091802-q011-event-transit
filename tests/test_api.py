"""HTTP API 冒烟：健康检查、只读重放、追溯与幂等命令。"""

import json
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

import service as service_module
from transit.events import EventStore
from transit.service import TransitService


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from transit.service import build_demo
        cls.svc = build_demo()
        service_module.Handler.svc = cls.svc
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), service_module.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _get(self, path):
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}{path}") as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def _post(self, path, body):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_health(self):
        status, payload = self._get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["service"], "event-transit")
        self.assertIn("Shanghai", payload["timezone"])

    def test_replay_and_trace_views(self):
        status, cap = self._get(
            "/api/replay/capacity?as_of=2026-09-20T07:10:00%2B08:00")
        self.assertEqual(status, 200)
        self.assertTrue(any(r["trip_id"] == "T3" and r["route_id"] == "BUS99"
                            for r in cap))
        status, tr = self._get("/api/trace/cong-BINHE")
        self.assertEqual(status, 200)
        self.assertEqual(tr["vehicle"]["vehicle_id"], "V-BUS-2")

    def test_command_idempotent_via_api(self):
        body = {"runner_id": "R-API", "wave_id": "A",
                "at": "2026-09-18T10:00:00+08:00", "event_id": "api:R-API"}
        s1, r1 = self._post("/api/command/register_runner", body)
        s2, r2 = self._post("/api/command/register_runner", body)
        self.assertEqual((s1, s2), (200, 200))
        events = [e for e in self.svc.events() if e["event_id"] == "api:R-API"]
        self.assertEqual(len(events), 1)

    def test_unknown_route_and_bad_command(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._get("/nope")
        self.assertEqual(cm.exception.code, 404)
        status, payload = self._post("/api/command/register_runner", {})
        self.assertEqual(status, 400)
        self.assertIn("缺少必填参数", payload["error"])


if __name__ == "__main__":
    unittest.main()
