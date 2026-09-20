"""赛事交通权益联动的服务入口（HTTP API）。

GET（只读重放，?as_of= 可回到任一时刻，按 Asia/Shanghai 解释）：
  /health                      服务身份
  /api/replay/capacity         各时刻可用运力（普通接驳/医疗分列）
  /api/replay/redemptions      免费乘车核销
  /api/replay/undelivered      未送达站点通知
  /api/replay/entitlements     报名/号码布/批次/窗口状态
  /api/replay/dispatch         线路/车辆/班次/管制/医疗状态
  /api/alerts                  拥堵与满员告警
  /api/trace/<alert_id>        告警沿批次→车辆→路段→处置决定的链路
  /api/events                  原始事件日志

POST（命令，全部走幂等事件日志）：
  /api/command/<action>        action 见 COMMAND_HANDLERS

运行：
  python3 service.py --demo                 跑演示场景并打印重放与追溯摘要
  python3 service.py --demo --serve --port  以演示数据启动 HTTP 服务
  python3 service.py --serve --store a.jsonl 空服务 + JSONL 持久化
  python3 service.py --check                基础契约自检
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from transit import EVENT_TIMEZONE
from transit.events import EventStore
from transit.service import TransitService, build_demo

SERVICE_ID = "event-transit"
SERVICE_NAME = "赛事交通权益联动"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME,
            "timezone": str(EVENT_TIMEZONE)}


# ---- 命令分发表 ------------------------------------------------------------

COMMAND_HANDLERS = {
    # 权益域
    "configure_wave":      ("e", "configure_wave", ["wave_id", "label", "start_at"]),
    "register_runner":     ("e", "register_runner", ["runner_id", "wave_id", "at"]),
    "change_wave":         ("e", "change_wave", ["runner_id", "to_wave", "at"]),
    "issue_bib":           ("e", "issue_bib", ["runner_id", "bib", "at"]),
    "report_lost":         ("e", "report_lost", ["bib", "at"]),
    "issue_replacement":   ("e", "issue_replacement", ["runner_id", "new_bib", "at"]),
    "withdraw":            ("e", "withdraw", ["runner_id", "at"]),
    "open_window":         ("e", "open_window", ["window"]),
    "issue_snapshot":      ("e", "issue_snapshot", ["device_id", "at"]),
    "upload_offline_scans":("e", "upload_offline_scans", ["device_id", "scans"]),
    "redeem":              ("e", "redeem", ["bib", "window_id", "at"]),
    # 调度域
    "register_route":      ("d", "register_route",
                            ["route_id", "name", "mode", "stops", "at"]),
    "register_alternative":("d", "register_alternative",
                            ["segment_id", "alt_route_id", "description", "at"]),
    "register_vehicle":    ("d", "register_vehicle",
                            ["vehicle_id", "capacity", "at"]),
    "register_hospital":   ("d", "register_hospital",
                            ["hospital_id", "name", "at"]),
    "schedule_trip":       ("d", "schedule_trip",
                            ["trip_id", "route_id", "vehicle_id", "departure_at"]),
    "mark_departed":       ("d", "mark_departed", ["trip_id", "at"]),
    "mark_completed":      ("d", "mark_completed", ["trip_id", "at"]),
    "board":               ("d", "board",
                            ["trip_id", "bib", "at", "redeem_id"]),
    "impose_closure":      ("d", "impose_closure",
                            ["restriction_id", "segment_id", "start_at",
                             "end_at", "reason"]),
    "lift_closure":        ("d", "lift_closure", ["restriction_id", "at"]),
    "record_receipt":      ("d", "record_notification_receipt",
                            ["notification_id", "stop_id", "at"]),
    "raise_alert":         ("d", "raise_alert", ["alert_id", "kind", "at"]),
    "request_medical":     ("d", "request_medical_transport",
                            ["request_id", "pickup_stop", "hospital_id", "at"]),
    "deliver_patient":     ("d", "deliver_patient", ["request_id", "at"]),
}

REPLAY_VIEWS = {
    "capacity": "replay_capacity",
    "redemptions": "replay_redemptions",
    "undelivered": "replay_undelivered",
    "entitlements": "replay_entitlements",
    "dispatch": "replay_dispatch",
}


def execute_command(svc: TransitService, action: str, body: dict):
    if action not in COMMAND_HANDLERS:
        raise KeyError(f"未知命令: {action}")
    domain_key, method_name, required = COMMAND_HANDLERS[action]
    domain = svc.entitlements if domain_key == "e" else svc.dispatch
    missing = [k for k in required if k not in body]
    if missing:
        raise ValueError(f"缺少必填参数: {', '.join(missing)}")
    kwargs = {k: v for k, v in body.items() if k != "event_id"}
    if "event_id" in body:
        kwargs["event_id"] = body["event_id"]
    result = getattr(domain, method_name)(**kwargs)
    return _jsonable(result)


def _jsonable(value):
    if hasattr(value, "__dict__"):
        return {k: v for k, v in vars(value).items() if not k.startswith("_")}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    return value


class Handler(BaseHTTPRequestHandler):
    svc: TransitService = None  # 由 main 注入到类属性

    # ---- GET ------------------------------------------------------------

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        as_of = query.get("as_of", [None])[0]
        try:
            if path == "/health":
                return self._write_json(200, health_payload())
            if path == "/api/events":
                return self._write_json(200, self.svc.events(as_of))
            if path == "/api/alerts":
                return self._write_json(200, self.svc.alerts(as_of))
            if path.startswith("/api/trace/"):
                alert_id = path.rsplit("/", 1)[-1]
                return self._write_json(200, self.svc.trace(alert_id, as_of))
            if path.startswith("/api/replay/"):
                view = path.rsplit("/", 1)[-1]
                method = REPLAY_VIEWS.get(view)
                if method is None:
                    return self._write_error(404, f"未知重放视图: {view}")
                return self._write_json(200, getattr(self.svc, method)(as_of))
            self._write_error(404, "未找到路径")
        except LookupError as exc:
            self._write_error(404, str(exc))
        except Exception as exc:  # noqa: BLE001 - 接口统一报错
            self._write_error(400, str(exc))

    # ---- POST -----------------------------------------------------------

    def do_POST(self):
        parsed = urlparse(self.path)
        prefix = "/api/command/"
        if not parsed.path.startswith(prefix):
            return self._write_error(404, "命令请发往 /api/command/<action>")
        action = parsed.path[len(prefix):].strip("/")
        try:
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError as exc:
            return self._write_error(400, f"请求体不是合法 JSON: {exc}")
        try:
            result = execute_command(self.svc, action, body)
            self._write_json(200, {"ok": True, "action": action, "result": result})
        except KeyError as exc:
            self._write_error(404, str(exc).strip("'"))
        except (ValueError, LookupError) as exc:
            self._write_error(400, str(exc))

    # ---- 工具 -----------------------------------------------------------

    def _write_json(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _write_error(self, status: int, message: str) -> None:
        self._write_json(status, {"ok": False, "error": message})

    def log_message(self, *_args):
        return


# ---- 演示与自检 ------------------------------------------------------------

def demo_summary(svc: TransitService) -> dict:
    """跑一遍关键重放/追溯查询，供 --demo 与自检使用。"""
    cap = svc.replay_capacity("2026-09-20T07:10:00+08:00")
    rides = svc.replay_redemptions("2026-09-20T09:00:00+08:00")
    undelivered = svc.replay_undelivered("2026-09-20T11:00:00+08:00")
    trace = svc.trace("cong-BINHE")
    return {
        "capacity_at_0710": cap,
        "redemptions_by_0900": rides,
        "undelivered_by_1100": undelivered,
        "trace_cong_BINHE": trace,
    }


def _self_check() -> None:
    assert health_payload()["service"] == SERVICE_ID
    svc = build_demo()
    summary = demo_summary(svc)

    trips = {row["trip_id"]: row for row in summary["capacity_at_0710"]}
    # T1 满员且已发车；T3 因滨河路封闭改道 BUS99；医疗班不被取消
    assert trips["T1"]["occupied"] == 3 and trips["T1"]["available"] == 0
    assert trips["T3"]["status"] == "diverted"
    assert trips["T3"]["route_id"] == "BUS99"
    assert trips["TMED"]["kind"] == "medical" and trips["TMED"]["status"] == "scheduled"

    rides = {(r["bib"], r["window_id"], r["at"][11:16]): r
             for r in summary["redemptions_by_0900"]}
    # 挂失前的公交通行保留；退赛后的核销拒绝
    assert rides[("B0004", "bib-bus", "06:10")]["accepted"] is True
    assert rides[("B1004", "bib-bus", "08:30")]["accepted"] is False
    # 改枪前后两笔地铁核销的批次归属不同
    wave_at = {r["at"][11:16]: r["wave_id"]
               for r in summary["redemptions_by_0900"] if r["bib"] == "C0003"}
    assert wave_at["05:30"] == "C" and wave_at["06:20"] == "B"
    # 离线重放不重复计数
    dup = [r for r in summary["redemptions_by_0900"] if r["event_id"] == "off:A0002:1"]
    assert len(dup) == 1
    # 挂失后离线放行的记录保留但带差异告警
    mismatch = next(r for r in summary["redemptions_by_0900"]
                    if r["event_id"] == "off:B0004:1")
    assert mismatch["accepted"] is True
    assert any(w.startswith("offline_basis_mismatch") for w in mismatch["warnings"])

    # 未送达通知只含缺回执站点（太原南站）
    pending = summary["undelivered_by_1100"][0]["pending_stops"]
    assert pending == ["太原南站"]

    # 追溯链覆盖 批次→车辆→路段→决定→通知→医疗
    tr = summary["trace_cong_BINHE"]
    assert tr["vehicle"]["vehicle_id"] == "V-BUS-2"
    assert any(w["wave_id"] == "A" for w in tr["wave_lineage"])
    assert tr["segment_lineage"]["closures"][0]["restriction_id"] == "R-918"
    assert tr["segment_lineage"]["closures"][0]["alt_route_id"] == "BUS99"
    decision_types = {d["type"] for d in tr["decisions"]}
    assert "TripDiverted" in decision_types
    assert any(m["request"]["request_id"] == "M-1" for m in tr["medical"])
    print("基础检查通过")


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true", help="契约自检")
    parser.add_argument("--demo", action="store_true", help="构建四枪发令演示场景")
    parser.add_argument("--serve", action="store_true", help="启动 HTTP 服务")
    parser.add_argument("--store", default=None, help="事件日志 JSONL 路径")
    args = parser.parse_args()

    if args.check:
        _self_check()
        return

    if args.demo:
        svc = build_demo(args.store)
        if not args.serve:
            print(json.dumps(demo_summary(svc), ensure_ascii=False,
                             indent=2, default=str))
            return
    else:
        svc = TransitService(EventStore(args.store))

    if args.serve or not args.demo:
        Handler.svc = svc
        server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
        print(f"{SERVICE_NAME} 监听 :{args.port}（时区 {EVENT_TIMEZONE}）")
        server.serve_forever()


if __name__ == "__main__":
    main()
