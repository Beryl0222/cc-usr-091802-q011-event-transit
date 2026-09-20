"""调度域：线路班次、车辆容量、临时管制、改派、通知回执、医疗优先。

硬性规则：
- 容量占用以幂等编号（redeem_id）去重，同一笔通行重放/重传只算一次。
- 普通接驳车辆与医疗转运车辆分池：普通乘客不能上医疗车，普通改派
  也不会征用医疗运力。
- 道路封闭只处置“尚未发车”的班次：已发车/已完成班次原样保留；
  未发班次按登记的替代路线改道，否则取消，并向受影响站点下发
  替代路线通知；站点回执逐条记录，缺回执即为未送达。
- 医疗转运不因普通管制被取消，触发医院绿色通道并单独留痕。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .clock import parse_event_time
from .events import EventStore

# ---- 事件类型 -------------------------------------------------------------

ROUTE_REGISTERED = "RouteRegistered"
ALTERNATIVE_REGISTERED = "AlternativeRouteRegistered"
VEHICLE_REGISTERED = "VehicleRegistered"
HOSPITAL_REGISTERED = "HospitalRegistered"
TRIP_SCHEDULED = "TripScheduled"
TRIP_DEPARTED = "TripDeparted"
TRIP_COMPLETED = "TripCompleted"
TRIP_DIVERTED = "TripDiverted"
TRIP_CANCELLED = "TripCancelled"
BOARDING_ACCEPTED = "BoardingAccepted"
BOARDING_REJECTED = "BoardingRejected"
ROAD_CLOSURE_IMPOSED = "RoadClosureImposed"
ROAD_CLOSURE_LIFTED = "RoadClosureLifted"
SITE_NOTIFICATION_ISSUED = "SiteNotificationIssued"
SITE_NOTIFICATION_DELIVERED = "SiteNotificationDelivered"
ALERT_RAISED = "AlertRaised"
MEDICAL_REQUESTED = "MedicalTransportRequested"
MEDICAL_ASSIGNED = "MedicalTransportAssigned"
PATIENT_DELIVERED = "PatientDelivered"
GREEN_CHANNEL_OPENED = "GreenChannelOpened"

BOARDABLE_STATUSES = {"scheduled", "boarding", "diverted"}


@dataclass
class BoardingResult:
    accepted: bool
    reason: str | None
    event_id: str
    occupied: int
    capacity: int


class DispatchDomain:
    def __init__(self, store: EventStore):
        self.store = store

    # ---- 基础网络 -------------------------------------------------------

    def register_route(self, route_id, name, mode, stops, at, event_id=None):
        """登记线路。stops 为有序站点；相邻站点自动构成路段 route_id:i。"""
        at = parse_event_time(at)
        segments = [
            {"segment_id": f"{route_id}:{i}", "from_stop": a, "to_stop": b}
            for i, (a, b) in enumerate(zip(stops, stops[1:]))
        ]
        event, _ = self.store.append(
            ROUTE_REGISTERED,
            {"route_id": route_id, "name": name, "mode": mode,
             "stops": list(stops), "segments": segments},
            at, event_id=event_id,
        )
        return event

    def register_alternative(self, segment_id, alt_route_id, description, at, event_id=None):
        """为某封闭路段登记替代线路（如地铁停运时的公交摆渡）。"""
        at = parse_event_time(at)
        event, _ = self.store.append(
            ALTERNATIVE_REGISTERED,
            {"segment_id": segment_id, "alt_route_id": alt_route_id,
             "description": description},
            at, event_id=event_id,
        )
        return event

    def register_vehicle(self, vehicle_id, capacity, at, medical=False,
                         route_id=None, label=None, event_id=None):
        at = parse_event_time(at)
        kind = "medical" if medical else "regular"
        event, _ = self.store.append(
            VEHICLE_REGISTERED,
            {"vehicle_id": vehicle_id, "label": label or vehicle_id,
             "capacity": int(capacity), "kind": kind, "route_id": route_id},
            at, event_id=event_id,
        )
        return event

    def register_hospital(self, hospital_id, name, at, event_id=None):
        at = parse_event_time(at)
        event, _ = self.store.append(
            HOSPITAL_REGISTERED,
            {"hospital_id": hospital_id, "name": name},
            at, event_id=event_id,
        )
        return event

    # ---- 班次 -----------------------------------------------------------

    def schedule_trip(self, trip_id, route_id, vehicle_id, departure_at,
                      waves_served=None, note="", event_id=None,
                      scheduled_at=None):
        at = parse_event_time(scheduled_at) if scheduled_at \
            else parse_event_time(departure_at)
        state = build_dispatch_state(self.store, as_of=at)
        if route_id not in state["routes"]:
            raise LookupError(f"线路不存在: {route_id}")
        if vehicle_id not in state["vehicles"]:
            raise LookupError(f"车辆不存在: {vehicle_id}")
        event, _ = self.store.append(
            TRIP_SCHEDULED,
            {"trip_id": trip_id, "route_id": route_id, "vehicle_id": vehicle_id,
             "departure_at": at.isoformat(), "waves_served": waves_served or [],
             "note": note, "status": "scheduled"},
            at, event_id=event_id,
        )
        return event

    def mark_departed(self, trip_id, at, event_id=None):
        at = parse_event_time(at)
        state = build_dispatch_state(self.store, as_of=at)
        trip = state["trips"].get(trip_id)
        if trip is None:
            raise LookupError(f"班次不存在: {trip_id}")
        if trip["status"] not in BOARDABLE_STATUSES:
            raise ValueError(f"班次当前状态 {trip['status']} 不可发车")
        event, _ = self.store.append(
            TRIP_DEPARTED, {"trip_id": trip_id, "route_id": trip["route_id"]},
            at, event_id=event_id,
        )
        return event

    def mark_completed(self, trip_id, at, event_id=None):
        at = parse_event_time(at)
        event, _ = self.store.append(
            TRIP_COMPLETED, {"trip_id": trip_id}, at, event_id=event_id,
        )
        return event

    def board(self, trip_id, bib, at, redeem_id, stop_id=None, event_id=None):
        """乘客登乘，占用一个座位。

        - 幂等：同一 redeem_id 重放返回既有结果，不重复占座；
        - 已发车/已取消/已改道完成的班次拒绝；
        - 医疗车拒绝普通接驳乘客（medical_resource_reserved）；
        - 满员拒绝（capacity_full），拒绝同样留痕但不占容量。
        """
        at = parse_event_time(at)
        if self.store.get(redeem_id) is not None:
            state = build_dispatch_state(self.store, as_of=at)
            trip = state["trips"].get(trip_id)
            if trip is None:
                return BoardingResult(True, None, redeem_id, 0, 0)
            cap = state["vehicles"].get(trip["vehicle_id"], {}).get("capacity", 0)
            return BoardingResult(True, None, redeem_id, len(trip["occupied"]), cap)

        state = build_dispatch_state(self.store, as_of=at)
        trip = state["trips"].get(trip_id)
        if trip is None:
            return self._reject_boarding(trip_id, bib, at, redeem_id, stop_id, "unknown_trip")
        vehicle = state["vehicles"][trip["vehicle_id"]]
        if vehicle["kind"] == "medical":
            return self._reject_boarding(trip_id, bib, at, redeem_id, stop_id,
                                         "medical_resource_reserved")
        if trip["status"] not in BOARDABLE_STATUSES:
            return self._reject_boarding(trip_id, bib, at, redeem_id, stop_id,
                                         f"trip_{trip['status']}")
        if len(trip["occupied"]) >= vehicle["capacity"]:
            self._raise_capacity_alert_if_needed(trip, at)
            return self._reject_boarding(trip_id, bib, at, redeem_id, stop_id, "capacity_full")

        event, _ = self.store.append(
            BOARDING_ACCEPTED,
            {"trip_id": trip_id, "vehicle_id": trip["vehicle_id"],
             "route_id": trip.get("effective_route") or trip["route_id"],
             "bib": bib, "redeem_id": redeem_id, "stop_id": stop_id},
            at, event_id=event_id or redeem_id,
        )
        state = build_dispatch_state(self.store, as_of=at)
        trip = state["trips"][trip_id]
        return BoardingResult(True, None, event.event_id,
                              len(trip["occupied"]), vehicle["capacity"])

    def _reject_boarding(self, trip_id, bib, at, redeem_id, stop_id, reason):
        event, _ = self.store.append(
            BOARDING_REJECTED,
            {"trip_id": trip_id, "bib": bib, "redeem_id": redeem_id,
             "stop_id": stop_id, "reason": reason},
            at, event_id=f"reject:{trip_id}:{redeem_id}",
        )
        return BoardingResult(False, reason, event.event_id, 0, 0)

    def _raise_capacity_alert_if_needed(self, trip, at):
        alert_id = f"cap:{trip['trip_id']}"
        state = build_dispatch_state(self.store, as_of=at)
        if any(a["alert_id"] == alert_id for a in state["alerts"]):
            return
        self.raise_alert(alert_id, "capacity", at, severity="high",
                         detail=f"班次 {trip['trip_id']} 满员", trip_id=trip["trip_id"])

    # ---- 管制与改派 -----------------------------------------------------

    def impose_closure(self, restriction_id, segment_id, start_at, end_at,
                       reason, alt_route_id=None, event_id=None):
        """对某路段实施封闭，并立即处置所有尚未发车的受影响班次。

        同一 restriction_id 重放为幂等：已派生的改道/取消/通知不重复产生。
        """
        start = parse_event_time(start_at)
        end = parse_event_time(end_at)
        state = build_dispatch_state(self.store, as_of=start)
        existing = [
            e for e in self.store.all()
            if e.event_type == ROAD_CLOSURE_IMPOSED
            and e.payload.get("restriction_id") == restriction_id
        ]
        if existing:
            return existing[0]

        segment = _find_segment(state, segment_id)
        if segment is None:
            raise LookupError(f"路段不存在: {segment_id}")
        alt = alt_route_id
        if alt is None:
            for alt_ev in self.store.replay(start, types=[ALTERNATIVE_REGISTERED]):
                if alt_ev.payload["segment_id"] == segment_id:
                    alt = alt_ev.payload["alt_route_id"]
                    break

        closure, _ = self.store.append(
            ROAD_CLOSURE_IMPOSED,
            {"restriction_id": restriction_id, "segment_id": segment_id,
             "route_id": segment["route_id"], "from_stop": segment["from_stop"],
             "to_stop": segment["to_stop"], "start_at": start.isoformat(),
             "end_at": end.isoformat(), "reason": reason,
             "alt_route_id": alt},
            start, event_id=event_id,
        )

        affected_stops = _downstream_stops(state, segment)
        affected_trips = [
            t for t in state["trips"].values()
            if t["route_id"] == segment["route_id"]
            and t["status"] in BOARDABLE_STATUSES
            and _trip_crosses_segment(t, segment, state)
        ]
        diverted, cancelled = [], []
        for trip in affected_trips:
            if state["vehicles"][trip["vehicle_id"]]["kind"] == "medical":
                # 医疗转运不走普通取消/改派池，保留原任务并优先通知
                continue
            if alt:
                self.store.append(
                    TRIP_DIVERTED,
                    {"trip_id": trip["trip_id"], "restriction_id": restriction_id,
                     "original_route_id": trip["route_id"], "new_route_id": alt},
                    start, event_id=f"divert:{restriction_id}:{trip['trip_id']}",
                )
                diverted.append(trip["trip_id"])
            else:
                self.store.append(
                    TRIP_CANCELLED,
                    {"trip_id": trip_id_guard(trip), "restriction_id": restriction_id,
                     "reason": reason},
                    start, event_id=f"cancel:{restriction_id}:{trip['trip_id']}",
                )
                cancelled.append(trip["trip_id"])

        message = _closure_message(segment, reason, alt, diverted, cancelled)
        self.store.append(
            SITE_NOTIFICATION_ISSUED,
            {"notification_id": f"notice:{restriction_id}",
             "restriction_id": restriction_id, "stop_ids": affected_stops,
             "alt_route_id": alt, "diverted_trips": diverted,
             "cancelled_trips": cancelled, "message": message,
             "medical_priority": True},
            start, event_id=f"notice:{restriction_id}",
        )
        return closure

    def lift_closure(self, restriction_id, at, event_id=None):
        at = parse_event_time(at)
        event, _ = self.store.append(
            ROAD_CLOSURE_LIFTED, {"restriction_id": restriction_id},
            at, event_id=event_id,
        )
        return event

    def record_notification_receipt(self, notification_id, stop_id, at,
                                    channel="signage", event_id=None):
        """站点设备/值班员回执，证明通知已送达。"""
        at = parse_event_time(at)
        event, _ = self.store.append(
            SITE_NOTIFICATION_DELIVERED,
            {"notification_id": notification_id, "stop_id": stop_id, "channel": channel},
            at, event_id=event_id or f"receipt:{notification_id}:{stop_id}",
        )
        return event

    # ---- 告警与医疗 -----------------------------------------------------

    def raise_alert(self, alert_id, kind, at, severity="medium", detail="",
                    segment_id=None, trip_id=None, event_id=None):
        at = parse_event_time(at)
        event, _ = self.store.append(
            ALERT_RAISED,
            {"alert_id": alert_id, "kind": kind, "severity": severity,
             "detail": detail, "segment_id": segment_id, "trip_id": trip_id},
            at, event_id=event_id or f"alert:{alert_id}",
        )
        return event

    def request_medical_transport(self, request_id, pickup_stop, hospital_id,
                                  at, severity="urgent", incident_id=None,
                                  event_id=None):
        """请求医疗转运：从医疗车辆池分配运力并打开医院绿色通道。

        普通接驳车辆永不进入此分配；没有空闲医疗车时事件仍登记，
        assignment 为 None（等待增援），便于追溯。
        """
        at = parse_event_time(at)
        state = build_dispatch_state(self.store, as_of=at)
        if hospital_id not in state["hospitals"]:
            raise LookupError(f"医院不存在: {hospital_id}")
        self.store.append(
            MEDICAL_REQUESTED,
            {"request_id": request_id, "pickup_stop": pickup_stop,
             "hospital_id": hospital_id, "severity": severity,
             "incident_id": incident_id},
            at, event_id=event_id or f"medreq:{request_id}",
        )
        vehicle_id = _pick_medical_vehicle(state)
        self.store.append(
            MEDICAL_ASSIGNED,
            {"request_id": request_id, "vehicle_id": vehicle_id},
            at, event_id=f"medassign:{request_id}",
        )
        self.store.append(
            GREEN_CHANNEL_OPENED,
            {"hospital_id": hospital_id, "request_id": request_id},
            at, event_id=f"green:{request_id}",
        )
        return {"request_id": request_id, "vehicle_id": vehicle_id,
                "hospital_id": hospital_id}

    def deliver_patient(self, request_id, at, event_id=None):
        at = parse_event_time(at)
        event, _ = self.store.append(
            PATIENT_DELIVERED, {"request_id": request_id},
            at, event_id=event_id or f"delivered:{request_id}",
        )
        return event


# ---- 投影 ------------------------------------------------------------------

def build_dispatch_state(store: EventStore, as_of=None) -> dict:
    routes, vehicles, hospitals, trips = {}, {}, {}, {}
    alternatives: dict[str, list] = {}
    active_restrictions: dict[str, dict] = {}
    alerts: list[dict] = []
    notifications: dict[str, dict] = {}
    medical: dict[str, dict] = {}

    for event in store.replay(as_of):
        p = event.payload
        t = event.event_type
        if t == ROUTE_REGISTERED:
            routes[p["route_id"]] = dict(p)
        elif t == ALTERNATIVE_REGISTERED:
            alternatives.setdefault(p["segment_id"], []).append(dict(p))
        elif t == VEHICLE_REGISTERED:
            vehicles[p["vehicle_id"]] = dict(p)
        elif t == HOSPITAL_REGISTERED:
            hospitals[p["hospital_id"]] = dict(p)
        elif t == TRIP_SCHEDULED:
            trips[p["trip_id"]] = {
                "trip_id": p["trip_id"], "route_id": p["route_id"],
                "vehicle_id": p["vehicle_id"],
                "departure_at": p["departure_at"],
                "waves_served": p.get("waves_served", []),
                "note": p.get("note", ""), "status": "scheduled",
                "occupied": set(), "boarding_events": [],
                "effective_route": p["route_id"], "restriction_id": None,
            }
        elif t == TRIP_DEPARTED and p["trip_id"] in trips:
            trips[p["trip_id"]]["status"] = "departed"
        elif t == TRIP_COMPLETED and p["trip_id"] in trips:
            trips[p["trip_id"]]["status"] = "completed"
        elif t == TRIP_DIVERTED and p["trip_id"] in trips:
            trip = trips[p["trip_id"]]
            trip["status"] = "diverted"
            trip["effective_route"] = p["new_route_id"]
            trip["restriction_id"] = p["restriction_id"]
        elif t == TRIP_CANCELLED and p["trip_id"] in trips:
            trip = trips[p["trip_id"]]
            trip["status"] = "cancelled"
            trip["restriction_id"] = p["restriction_id"]
        elif t == BOARDING_ACCEPTED:
            trip = trips.get(p["trip_id"])
            if trip is not None:
                trip["occupied"].add(p["redeem_id"])
                trip["boarding_events"].append({
                    "redeem_id": p["redeem_id"], "bib": p["bib"],
                    "stop_id": p.get("stop_id"), "at": event.event_time.isoformat(),
                })
        elif t == ROAD_CLOSURE_IMPOSED:
            active_restrictions[p["restriction_id"]] = dict(p)
        elif t == ROAD_CLOSURE_LIFTED:
            active_restrictions.pop(p["restriction_id"], None)
        elif t == SITE_NOTIFICATION_ISSUED:
            notifications[p["notification_id"]] = {
                **p, "issued_at": event.event_time.isoformat(),
                "delivered": {},
            }
        elif t == SITE_NOTIFICATION_DELIVERED:
            notice = notifications.get(p["notification_id"])
            if notice is not None:
                notice["delivered"][p["stop_id"]] = {
                    "at": event.event_time.isoformat(), "channel": p.get("channel"),
                }
        elif t == ALERT_RAISED:
            alerts.append({**p, "at": event.event_time.isoformat(), "event_id": event.event_id})
        elif t == MEDICAL_REQUESTED:
            medical[p["request_id"]] = {"request": dict(p), "vehicle_id": None,
                                       "delivered": False, "at": event.event_time}
        elif t == MEDICAL_ASSIGNED:
            if p["request_id"] in medical:
                medical[p["request_id"]]["vehicle_id"] = p["vehicle_id"]
        elif t == PATIENT_DELIVERED:
            if p["request_id"] in medical:
                medical[p["request_id"]]["delivered"] = True
        elif t == GREEN_CHANNEL_OPENED:
            hospitals.setdefault(p["hospital_id"], {"hospital_id": p["hospital_id"]})
            hospitals[p["hospital_id"]].setdefault("green_channels", []).append(
                {"request_id": p["request_id"], "opened_at": event.event_time.isoformat()})

    for trip in trips.values():
        trip["occupied"] = set(trip["occupied"])
    return {
        "routes": routes, "vehicles": vehicles, "hospitals": hospitals,
        "trips": trips, "alternatives": alternatives,
        "active_restrictions": active_restrictions, "alerts": alerts,
        "notifications": notifications, "medical": medical,
    }


def capacity_snapshot(store: EventStore, as_of=None) -> list[dict]:
    """重放某时刻各班次的可用运力（普通接驳与医疗转运分列）。"""
    state = build_dispatch_state(store, as_of)
    rows = []
    for trip_id, trip in sorted(state["trips"].items()):
        vehicle = state["vehicles"][trip["vehicle_id"]]
        occupied = len(trip["occupied"])
        rows.append({
            "trip_id": trip_id,
            "status": trip["status"],
            "route_id": trip["effective_route"],
            "original_route_id": trip["route_id"],
            "vehicle_id": trip["vehicle_id"],
            "kind": vehicle["kind"],
            "capacity": vehicle["capacity"],
            "occupied": occupied,
            "available": max(vehicle["capacity"] - occupied, 0)
                if trip["status"] in BOARDABLE_STATUSES else 0,
            "waves_served": trip["waves_served"],
            "departure_at": trip["departure_at"],
            "restriction_id": trip["restriction_id"],
        })
    return rows


def undelivered_notifications(store: EventStore, as_of=None) -> list[dict]:
    """截至某时刻仍缺站点回执的通知（按站点列出未送达明细）。"""
    state = build_dispatch_state(store, as_of)
    result = []
    for notice in state["notifications"].values():
        pending = [stop for stop in notice["stop_ids"] if stop not in notice["delivered"]]
        if pending:
            result.append({
                "notification_id": notice["notification_id"],
                "restriction_id": notice.get("restriction_id"),
                "issued_at": notice["issued_at"],
                "message": notice["message"],
                "pending_stops": pending,
                "delivered_stops": list(notice["delivered"].keys()),
            })
    return result


# ---- 辅助函数 --------------------------------------------------------------

def _find_segment(state, segment_id):
    for route_id, route in state["routes"].items():
        for seg in route["segments"]:
            if seg["segment_id"] == segment_id:
                return {**seg, "route_id": route_id,
                        "index": route["segments"].index(seg)}
    return None


def _downstream_stops(state, segment) -> list[str]:
    route = state["routes"][segment["route_id"]]
    start_index = route["stops"].index(segment["from_stop"])
    return route["stops"][start_index:]


def _trip_crosses_segment(trip, segment, state) -> bool:
    route = state["routes"].get(trip["route_id"])
    if not route or trip["effective_route"] != segment["route_id"]:
        return False
    stop_index = segment["index"] + 1  # 封闭段的终点及以后不可达
    destination = route["stops"][-1]
    return route["stops"].index(destination) >= stop_index


def _pick_medical_vehicle(state):
    """选一辆当前没有未完成转运任务的医疗车；没有则 None。"""
    busy = {
        m["vehicle_id"] for m in state["medical"].values()
        if m["vehicle_id"] and not m["delivered"]
    }
    for vehicle_id, vehicle in state["vehicles"].items():
        if vehicle["kind"] == "medical" and vehicle_id not in busy:
            return vehicle_id
    return None


def _closure_message(segment, reason, alt, diverted, cancelled):
    via = f"，请改乘替代线路 {alt}" if alt else "，该方向暂无替代线路"
    parts = [f"{segment['from_stop']}—{segment['to_stop']} 路段封闭（{reason}）{via}"]
    if diverted:
        parts.append(f"改道班次 {len(diverted)} 个")
    if cancelled:
        parts.append(f"取消班次 {len(cancelled)} 个")
    return "；".join(parts)


def trip_id_guard(trip):
    return trip["trip_id"]
