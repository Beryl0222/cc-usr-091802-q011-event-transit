"""HTTP 接口：把权益与调度服务暴露为 JSON API。

约定：请求/响应均为 UTF-8 JSON；所有时间传 ISO8601（建议带 ``+08:00``）。
重放同一业务记录返回 ``200`` 且 ``status="duplicate"``（幂等，不产生第二次效果）；
其余业务冲突返回 409，找不到资源 404，参数错误 422。
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import demo as demo_mod
from .dispatch import DispatchService
from .entitlements import EntitlementService
from .errors import DuplicateReplay, NotFound, TransitError
from .events import EventLog
from .trace import TraceService


class Hub:
    """共享同一条事件日志的全部领域服务。"""

    def __init__(self, store: str | None = None):
        self.log = EventLog(store)
        self.entitlements = EntitlementService(self.log)
        self.dispatch = DispatchService(self.log)
        self.trace = TraceService(self.log)


def _ok(value) -> dict:
    return {"ok": True, "result": value}


class Handler(BaseHTTPRequestHandler):
    hub: Hub  # 由工厂函数注入到子类

    # ------------------------------------------------------------ 基础框架

    def _send(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise TransitError(f"请求体不是合法 JSON：{exc}", "bad_json")
        if not isinstance(data, dict):
            raise TransitError("请求体必须是 JSON 对象", "bad_json")
        return data

    def _query(self) -> dict:
        q = parse_qs(urlparse(self.path).query)
        return {k: v[-1] for k, v in q.items()}

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _route(self, method: str):
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            handler = ROUTES.get((method, path))
            params = {}
            if handler is None:
                handler, params = self._match(method, path)
            if handler is None:
                if method == "GET" and path == "/health":
                    self._send(200, {"status": "ok", "service": "event-transit",
                                     "name": "赛事交通权益联动"})
                    return
                self._send(404, {"ok": False, "error": {"code": "not_found",
                                                        "message": f"无此接口 {method} {path}"}})
                return
            result = handler(self, **params)
            if result is not None:
                self._send(200, _ok(result))
        except DuplicateReplay as exc:
            # 幂等重放：返回首次事件，客户端可安全重试
            self._send(200, {"ok": True, "status": "duplicate",
                             "code": exc.code, "message": str(exc),
                             "original": exc.details.get("event")})
        except NotFound as exc:
            self._send(404, {"ok": False, "error": {"code": exc.code, "message": str(exc)}})
        except TransitError as exc:
            status = 409 if exc.code in ("conflict",) or exc.code.endswith("_exists") \
                or exc.code.endswith("_closed") or exc.code in (
                    "capacity_exceeded", "medical_capacity_exceeded",
                    "trip_already_departed", "trip_cancelled", "route_blocked",
                    "same_wave", "bib_already_lost", "green_channel_closed",
                    "bib_already_held", "already_withdrawn") else 422
            self._send(status, {"ok": False,
                                "error": {"code": exc.code, "message": str(exc),
                                          "details": exc.details}})

    def _match(self, method: str, path: str):
        """匹配带路径参数的路由，如 /bibs/{bib}/entitlements。"""
        parts = path.split("/")
        for (m, template), fn in ROUTES.items():
            if m != method or "{" not in template:
                continue
            tparts = template.split("/")
            if len(tparts) != len(parts):
                continue
            params = {}
            for tp, pp in zip(tparts, parts):
                if tp.startswith("{") and tp.endswith("}"):
                    params[tp[1:-1]] = pp
                elif tp != pp:
                    break
            else:
                return fn, params
        return None, {}

    def log_message(self, *_args):
        return

    # ============================================================ 处理器

    # -- 配置/批次/窗口/设备 ----------------------------------------

    def h_create_wave(self):
        b = self._read_json()
        e = self.hub.entitlements
        ev = e.define_wave(b["wave_id"], b["name"], b["start"],
                           color=b.get("color", ""), occurred_at=b.get("occurred_at"))
        return ev.to_dict()

    def h_create_window(self):
        b = self._read_json()
        ev = self.hub.entitlements.define_window(
            b["window_id"], b["mode"], b["start"], b["end"],
            benefit=b.get("benefit", "free_ride"), waves=b.get("waves"),
            note=b.get("note", ""), occurred_at=b.get("occurred_at"))
        return ev.to_dict()

    def h_register_device(self):
        b = self._read_json()
        ev = self.hub.entitlements.register_device(
            b["device_id"], b["kind"], b.get("location", ""),
            occurred_at=b.get("occurred_at"))
        return ev.to_dict()

    # -- 选手/号码布 ------------------------------------------------

    def h_register_runner(self):
        b = self._read_json()
        ev = self.hub.entitlements.register_runner(
            b["registration_id"], b["name"], b["wave_id"],
            occurred_at=b.get("occurred_at"))
        return ev.to_dict()

    def h_issue_bib(self):
        b = self._read_json()
        ev = self.hub.entitlements.issue_bib(
            b["bib"], b["registration_id"], occurred_at=b.get("occurred_at"))
        return ev.to_dict()

    def h_lose_bib(self):
        b = self._read_json()
        ev = self.hub.entitlements.report_bib_lost(
            b["bib"], occurred_at=b.get("occurred_at"))
        return ev.to_dict()

    def h_replace_bib(self):
        b = self._read_json()
        ev = self.hub.entitlements.replace_bib(
            b["old_bib"], b["new_bib"], occurred_at=b.get("occurred_at"))
        return ev.to_dict()

    def h_withdraw(self):
        b = self._read_json()
        ev = self.hub.entitlements.withdraw(
            b["registration_id"], occurred_at=b.get("occurred_at"))
        return ev.to_dict()

    def h_change_wave(self):
        b = self._read_json()
        ev = self.hub.entitlements.change_wave(
            b["registration_id"], b["to_wave"], occurred_at=b.get("occurred_at"))
        return ev.to_dict()

    def h_bib_entitlements(self, bib: str):
        at = self._query().get("at")
        return self.hub.entitlements.entitlement_snapshot(bib, at)

    # -- 验票/离线 --------------------------------------------------

    def h_validate_ride(self):
        b = self._read_json()
        return self.hub.entitlements.validate_ride(
            b["device_id"], b["bib"], b["mode"], b.get("at"),
            ride_id=b.get("ride_id"), recorded_at=b.get("recorded_at"),
            online=b.get("online", True), bundle_id=b.get("bundle_id"),
            local_decision=b.get("local_decision"))

    def h_manifest(self, device_id: str):
        at = self._query().get("at")
        return self.hub.entitlements.device_manifest(device_id, at)

    def h_sync(self, device_id: str):
        b = self._read_json()
        return self.hub.entitlements.sync_device(
            device_id, b.get("records", []), recorded_at=b.get("recorded_at"))

    def h_ledger(self):
        q = self._query()
        return self.hub.entitlements.ride_ledger(q.get("start"), q.get("end"))

    # -- 路网/车辆/医院 ---------------------------------------------

    def h_create_stop(self):
        b = self._read_json()
        return self.hub.dispatch.define_stop(
            b["stop_id"], b["name"], occurred_at=b.get("occurred_at")).to_dict()

    def h_create_segment(self):
        b = self._read_json()
        return self.hub.dispatch.define_segment(
            b["segment_id"], b["from_stop"], b["to_stop"], b.get("name", ""),
            occurred_at=b.get("occurred_at")).to_dict()

    def h_create_route(self):
        b = self._read_json()
        return self.hub.dispatch.define_route(
            b["route_id"], b["name"], b["stop_ids"], b["segment_ids"],
            kind=b.get("kind", "shuttle"),
            occurred_at=b.get("occurred_at")).to_dict()

    def h_create_vehicle(self):
        b = self._read_json()
        return self.hub.dispatch.define_vehicle(
            b["vehicle_id"], b["plate"], int(b["capacity"]),
            kind=b.get("kind", "shuttle"),
            occurred_at=b.get("occurred_at")).to_dict()

    def h_create_hospital(self):
        b = self._read_json()
        return self.hub.dispatch.define_hospital(
            b["hospital_id"], b["name"], b["stop_id"],
            occurred_at=b.get("occurred_at")).to_dict()

    def h_green_channel(self):
        b = self._read_json()
        return self.hub.dispatch.set_green_channel(
            b["hospital_id"], bool(b["open"]),
            occurred_at=b.get("occurred_at")).to_dict()

    # -- 班次/容量/医疗 ---------------------------------------------

    def h_schedule_trip(self):
        b = self._read_json()
        return self.hub.dispatch.schedule_trip(
            b["trip_id"], b["route_id"], b["vehicle_id"], b["scheduled_departure"],
            occurred_at=b.get("occurred_at")).to_dict()

    def h_trip_status(self, trip_id: str):
        return self.hub.dispatch.trip_status(trip_id, self._query().get("at"))

    def h_depart_trip(self, trip_id: str):
        b = self._read_json()
        return self.hub.dispatch.depart_trip(
            trip_id, occurred_at=b.get("occurred_at")).to_dict()

    def h_complete_trip(self, trip_id: str):
        b = self._read_json()
        return self.hub.dispatch.complete_trip(
            trip_id, occurred_at=b.get("occurred_at")).to_dict()

    def h_board(self, trip_id: str):
        b = self._read_json()
        return self.hub.dispatch.board_shuttle(
            trip_id, b["ride_id"], int(b.get("seats", 1)),
            occurred_at=b.get("occurred_at"))

    def h_medical_reserve(self):
        b = self._read_json()
        return self.hub.dispatch.reserve_medical(
            b["trip_id"], b["case_ref"], int(b["seats"]), b["hospital_id"],
            occurred_at=b.get("occurred_at"))

    def h_medical_transport(self):
        b = self._read_json()
        return self.hub.dispatch.transport_medical(
            b["trip_id"], b["case_ref"], occurred_at=b.get("occurred_at"))

    # -- 封控/通知/告警 ---------------------------------------------

    def h_close_road(self):
        b = self._read_json()
        return self.hub.dispatch.close_road(
            b["closure_id"], b["segment_ids"], reason=b.get("reason", ""),
            occurred_at=b.get("occurred_at"))

    def h_reopen_road(self):
        b = self._read_json()
        return self.hub.dispatch.reopen_road(
            b["closure_id"], occurred_at=b.get("occurred_at")).to_dict()

    def h_receipt(self, notification_id: str):
        b = self._read_json()
        return self.hub.dispatch.record_receipt(
            notification_id, delivered=bool(b["delivered"]),
            channel=b.get("channel", "station_display"),
            detail=b.get("detail", ""), occurred_at=b.get("occurred_at")).to_dict()

    def h_report_alert(self):
        b = self._read_json()
        return self.hub.dispatch.report_congestion(
            b["alert_id"], b["segment_id"], b["severity"], note=b.get("note", ""),
            observed_at=b.get("observed_at"), source=b.get("source", "traffic-camera")
        ).to_dict()

    def h_handle_alert(self, alert_id: str):
        b = self._read_json()
        return self.hub.dispatch.handle_alert(
            alert_id, b["decision"], note=b.get("note", ""),
            refs=b.get("refs"), occurred_at=b.get("occurred_at")).to_dict()

    # -- 查询/溯源/重放 ---------------------------------------------

    def h_fleet(self):
        return self.hub.dispatch.fleet_snapshot(self._query().get("at"))

    def h_trace_alert(self, alert_id: str):
        return self.hub.trace.trace_alert(alert_id)

    def h_replay(self):
        q = self._query()
        return self.hub.trace.replay_at(q["at"], axis=q.get("axis", "occurred"))

    def h_events(self):
        return {"count": len(self.hub.log),
                "events": [e.to_dict() for e in self.hub.log.all()]}

    def h_load_demo(self):
        if len(self.hub.log) > 0:
            return {"loaded": 0, "skipped": "event_log_not_empty"}
        result = demo_mod.build(self.hub.log)
        return {"loaded": len(self.hub.log), "alert_id": result["alert_id"]}


ROUTES = {
    ("POST", "/waves"): Handler.h_create_wave,
    ("POST", "/windows"): Handler.h_create_window,
    ("POST", "/devices"): Handler.h_register_device,
    ("POST", "/runners"): Handler.h_register_runner,
    ("POST", "/bibs/issue"): Handler.h_issue_bib,
    ("POST", "/bibs/lost"): Handler.h_lose_bib,
    ("POST", "/bibs/replace"): Handler.h_replace_bib,
    ("POST", "/runners/withdraw"): Handler.h_withdraw,
    ("POST", "/runners/change-wave"): Handler.h_change_wave,
    ("GET", "/bibs/{bib}/entitlements"): Handler.h_bib_entitlements,

    ("POST", "/rides/validate"): Handler.h_validate_ride,
    ("GET", "/devices/{device_id}/manifest"): Handler.h_manifest,
    ("POST", "/devices/{device_id}/sync"): Handler.h_sync,
    ("GET", "/rides/ledger"): Handler.h_ledger,

    ("POST", "/stops"): Handler.h_create_stop,
    ("POST", "/segments"): Handler.h_create_segment,
    ("POST", "/routes"): Handler.h_create_route,
    ("POST", "/vehicles"): Handler.h_create_vehicle,
    ("POST", "/hospitals"): Handler.h_create_hospital,
    ("POST", "/hospitals/green-channel"): Handler.h_green_channel,

    ("POST", "/trips"): Handler.h_schedule_trip,
    ("GET", "/trips/{trip_id}"): Handler.h_trip_status,
    ("POST", "/trips/{trip_id}/depart"): Handler.h_depart_trip,
    ("POST", "/trips/{trip_id}/complete"): Handler.h_complete_trip,
    ("POST", "/trips/{trip_id}/board"): Handler.h_board,
    ("POST", "/medical/reserve"): Handler.h_medical_reserve,
    ("POST", "/medical/transport"): Handler.h_medical_transport,

    ("POST", "/roads/close"): Handler.h_close_road,
    ("POST", "/roads/reopen"): Handler.h_reopen_road,
    ("POST", "/notifications/{notification_id}/receipt"): Handler.h_receipt,
    ("POST", "/alerts"): Handler.h_report_alert,
    ("POST", "/alerts/{alert_id}/handle"): Handler.h_handle_alert,

    ("GET", "/fleet"): Handler.h_fleet,
    ("GET", "/alerts/{alert_id}/trace"): Handler.h_trace_alert,
    ("GET", "/replay"): Handler.h_replay,
    ("GET", "/events"): Handler.h_events,
    ("POST", "/demo/load"): Handler.h_load_demo,
}


def create_server(port: int = 8000, store: str | None = None,
                  load_demo: bool = False) -> ThreadingHTTPServer:
    hub = Hub(store)
    if load_demo and len(hub.log) == 0:
        demo_mod.build(hub.log)

    class BoundHandler(Handler):
        pass

    BoundHandler.hub = hub
    server = ThreadingHTTPServer(("0.0.0.0", port), BoundHandler)
    server.hub = hub
    return server
