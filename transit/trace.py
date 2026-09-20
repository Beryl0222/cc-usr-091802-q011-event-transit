"""拥堵告警链路追溯。

选取任一告警（满员、拥堵、管制衍生），沿
    发令批次 → 选手 → 车辆/班次 → 路段 → 处置决定（改派/取消/通知/医疗）
把相关事件聚成一条按原始时间排序的时间线，回答“来龙去脉”。

追溯用“事件发生当时”的投影还原批次归属，因此改枪、挂失等事后变化
不会篡改历史：某选手在告警时刻属于哪一枪，就显示哪一枪。
"""

from __future__ import annotations

from .clock import parse_event_time
from .dispatch import (
    ALERT_RAISED, BOARDING_ACCEPTED, GREEN_CHANNEL_OPENED, MEDICAL_ASSIGNED,
    MEDICAL_REQUESTED, PATIENT_DELIVERED, ROAD_CLOSURE_IMPOSED,
    ROAD_CLOSURE_LIFTED, SITE_NOTIFICATION_DELIVERED, SITE_NOTIFICATION_ISSUED,
    TRIP_CANCELLED, TRIP_COMPLETED, TRIP_DEPARTED, TRIP_DIVERTED,
    build_dispatch_state,
)
from .entitlements import RIDE_REDEEMED, build_entitlement_state
from .events import EventStore


def trace_alert(store: EventStore, alert_id: str, as_of=None) -> dict:
    alert_event = None
    for event in store.replay(as_of, types=[ALERT_RAISED]):
        if event.payload.get("alert_id") == alert_id:
            alert_event = event
    if alert_event is None:
        raise LookupError(f"告警不存在: {alert_id}")

    state = build_dispatch_state(store, as_of)
    ap = alert_event.payload
    scope = {"trip_id": ap.get("trip_id"), "segment_id": ap.get("segment_id")}

    trip = state["trips"].get(ap["trip_id"]) if ap.get("trip_id") else None
    segment_route_id = None
    if ap.get("segment_id"):
        for route in state["routes"].values():
            if any(s["segment_id"] == ap["segment_id"] for s in route["segments"]):
                segment_route_id = route["route_id"]
                scope["route_id"] = route["route_id"]
                scope["segment"] = next(
                    s for s in route["segments"] if s["segment_id"] == ap["segment_id"])

    route_id = (trip["route_id"] if trip else None) or segment_route_id

    # ---- 车辆 / 班次 ----------------------------------------------------

    vehicle = None
    if trip:
        vehicle = state["vehicles"].get(trip["vehicle_id"])
        scope["vehicle_id"] = trip["vehicle_id"]
        scope["trip"] = {
            "trip_id": trip["trip_id"], "status": trip["status"],
            "route_id": trip["effective_route"], "original_route_id": trip["route_id"],
            "departure_at": trip["departure_at"], "waves_served": trip["waves_served"],
            "capacity": vehicle["capacity"] if vehicle else None,
            "occupied": len(trip["occupied"]),
            "restriction_id": trip["restriction_id"],
        }

    # ---- 发令批次 → 选手 → 登乘记录 ------------------------------------

    boarding_events = _collect_boarding(store, trip["trip_id"] if trip else None,
                                        trip["vehicle_id"] if trip else None)
    wave_lineage: dict[str, list] = {}
    for be in boarding_events:
        ent_state = build_entitlement_state(store, as_of=be.event_time)
        bib = be.payload["bib"]
        info = ent_state["bibs"].get(bib)
        runner_id = info["runner_id"] if info else None
        wave_id = ent_state["runners"][runner_id]["wave_id"] if runner_id else None
        wave_lineage.setdefault(wave_id or "unknown", []).append({
            "bib": bib, "runner_id": runner_id,
            "redeem_id": be.payload["redeem_id"],
            "stop_id": be.payload.get("stop_id"),
            "boarded_at": be.event_time.isoformat(),
        })
    waves = [
        {"wave_id": wave_id, "boarded_count": len(members), "members": members}
        for wave_id, members in sorted(wave_lineage.items())
    ]

    # ---- 路段：封闭/解封、替代线路 -------------------------------------

    closures = _segment_closures(store, scope.get("segment_id"), route_id)

    # ---- 处置决定：改道/取消/通知/回执/医疗 -----------------------------

    decisions = _decisions(store, closures, trip["trip_id"] if trip else None)

    # ---- 时间线 ---------------------------------------------------------

    related = [alert_event]
    related += [e for e in boarding_events]
    related += closures["events"]
    related += decisions["events"]
    timeline = [_timeline_entry(e) for e in related]
    timeline.sort(key=lambda item: (parse_event_time(item["at"]), item["seq"]))

    return {
        "alert": {
            "alert_id": alert_id,
            "kind": ap.get("kind"), "severity": ap.get("severity"),
            "detail": ap.get("detail"),
            "raised_at": alert_event.event_time.isoformat(),
        },
        "scope": scope,
        "vehicle": vehicle,
        "wave_lineage": waves,
        "segment_lineage": {
            "route_id": route_id,
            "segment_id": scope.get("segment_id"),
            "closures": closures["rows"],
            "alternatives": state["alternatives"].get(scope["segment_id"], [])
                if scope.get("segment_id") else [],
        },
        "decisions": decisions["rows"],
        "notifications": decisions["notifications"],
        "medical": decisions["medical"],
        "timeline": timeline,
    }


def list_alerts(store: EventStore, as_of=None) -> list[dict]:
    state = build_dispatch_state(store, as_of)
    return state["alerts"]


# ---- 内部收集 --------------------------------------------------------------

def _collect_boarding(store, trip_id, vehicle_id):
    result = []
    for event in store.replay(types=[BOARDING_ACCEPTED, RIDE_REDEEMED]):
        p = event.payload
        if event.event_type == BOARDING_ACCEPTED:
            if (trip_id and p.get("trip_id") == trip_id) or \
               (vehicle_id and p.get("vehicle_id") == vehicle_id):
                result.append(event)
        elif event.event_type == RIDE_REDEEMED and p.get("trip_id") == trip_id:
            result.append(event)
    return result


def _segment_closures(store, segment_id, route_id):
    rows, events = [], []
    for event in store.replay(types=[ROAD_CLOSURE_IMPOSED, ROAD_CLOSURE_LIFTED]):
        p = event.payload
        if event.event_type == ROAD_CLOSURE_IMPOSED:
            if (segment_id and p["segment_id"] == segment_id) or \
               (route_id and p.get("route_id") == route_id):
                rows.append({
                    "restriction_id": p["restriction_id"],
                    "segment_id": p["segment_id"],
                    "start_at": p["start_at"], "end_at": p["end_at"],
                    "reason": p["reason"], "alt_route_id": p.get("alt_route_id"),
                })
                events.append(event)
        else:
            if any(r["restriction_id"] == p["restriction_id"] for r in rows):
                events.append(event)
    return {"rows": rows, "events": events}


def _decisions(store, closures, anchor_trip_id):
    restriction_ids = {r["restriction_id"] for r in closures["rows"]}
    rows, events = [], []
    notifications, medical = [], {}
    notice_ids = set()

    watch_types = {TRIP_DIVERTED, TRIP_CANCELLED, TRIP_DEPARTED, TRIP_COMPLETED,
                   SITE_NOTIFICATION_ISSUED, SITE_NOTIFICATION_DELIVERED,
                   MEDICAL_REQUESTED, MEDICAL_ASSIGNED, PATIENT_DELIVERED,
                   GREEN_CHANNEL_OPENED}
    state = build_dispatch_state(store)

    for event in store.replay(types=watch_types):
        p = event.payload
        t = event.event_type
        if t in (TRIP_DIVERTED, TRIP_CANCELLED):
            rid = p.get("restriction_id")
            if rid in restriction_ids or p.get("trip_id") == anchor_trip_id:
                rows.append({"at": event.event_time.isoformat(), "type": t,
                             "trip_id": p["trip_id"], "restriction_id": rid,
                             "new_route_id": p.get("new_route_id"),
                             "reason": p.get("reason")})
                events.append(event)
        elif t in (TRIP_DEPARTED, TRIP_COMPLETED) and p.get("trip_id") == anchor_trip_id:
            rows.append({"at": event.event_time.isoformat(), "type": t,
                         "trip_id": p["trip_id"]})
            events.append(event)
        elif t == SITE_NOTIFICATION_ISSUED and p.get("restriction_id") in restriction_ids:
            notice_ids.add(p["notification_id"])
            notice = state["notifications"].get(p["notification_id"], {})
            notifications.append({
                "notification_id": p["notification_id"],
                "restriction_id": p["restriction_id"],
                "issued_at": event.event_time.isoformat(),
                "message": p["message"],
                "alt_route_id": p.get("alt_route_id"),
                "stop_ids": p["stop_ids"],
                "delivered_stops": list(notice.get("delivered", {}).keys()),
                "pending_stops": [s for s in p["stop_ids"]
                                  if s not in notice.get("delivered", {})],
            })
            events.append(event)
        elif t == SITE_NOTIFICATION_DELIVERED and p["notification_id"] in notice_ids:
            events.append(event)
        elif t in (MEDICAL_REQUESTED, MEDICAL_ASSIGNED, PATIENT_DELIVERED,
                   GREEN_CHANNEL_OPENED):
            # 与受封闭影响站点相关的医疗转运：整条请求链（请求/派车/
            # 绿色通道/送达）都纳入决定链
            request_id = p.get("request_id")
            related = request_id in medical
            pickup = p.get("pickup_stop")
            if not related and pickup:
                affected_stops = set()
                for r in closures["rows"]:
                    affected_stops.update(_closure_stop_set(state, r["restriction_id"]))
                if pickup in affected_stops:
                    related = True
            if related and request_id:
                medical[request_id] = True
                events.append(event)

    med_rows = []
    for request_id in medical:
        info = state["medical"].get(request_id)
        if info:
            med_rows.append(info)
    return {"rows": rows, "events": events, "notifications": notifications,
            "medical": med_rows}


def _closure_stop_set(state, restriction_id):
    notice = next((n for n in state["notifications"].values()
                   if n.get("restriction_id") == restriction_id), None)
    return set(notice["stop_ids"]) if notice else set()


def _timeline_entry(event) -> dict:
    t = event.event_type
    p = event.payload
    summaries = {
        "AlertRaised": f"告警 {p.get('alert_id')}（{p.get('kind')}/{p.get('severity')}）：{p.get('detail')}",
        "BoardingAccepted": f"{p.get('bib')} 登乘 {p.get('trip_id')} 车辆 {p.get('vehicle_id')} @ {p.get('stop_id')}",
        "RideRedeemed": f"{p.get('bib')} 核销 {p.get('window_id')} -> {'放行' if p.get('accepted') else '拒绝:' + str(p.get('reason'))}",
        "RoadClosureImposed": f"路段封闭 {p.get('segment_id')}（{p.get('reason')}），替代线路 {p.get('alt_route_id')}",
        "RoadClosureLifted": f"管制解除 {p.get('restriction_id')}",
        "TripDiverted": f"班次 {p.get('trip_id')} 改道至 {p.get('new_route_id')}",
        "TripCancelled": f"班次 {p.get('trip_id')} 取消（{p.get('reason')}）",
        "TripDeparted": f"班次 {p.get('trip_id')} 已发车",
        "TripCompleted": f"班次 {p.get('trip_id')} 已完成",
        "SiteNotificationIssued": f"向站点下发通知 {p.get('notification_id')}：{p.get('message')}",
        "SiteNotificationDelivered": f"站点 {p.get('stop_id')} 回执送达 {p.get('notification_id')}",
        "MedicalTransportRequested": f"医疗转运请求 {p.get('request_id')} @ {p.get('pickup_stop')}",
        "MedicalTransportAssigned": f"医疗车 {p.get('vehicle_id')} 承接 {p.get('request_id')}",
        "PatientDelivered": f"伤员送达 {p.get('request_id')}",
        "GreenChannelOpened": f"医院 {p.get('hospital_id')} 开通绿色通道（{p.get('request_id')}）",
    }
    return {
        "seq": event.seq,
        "at": event.event_time.isoformat(),
        "recorded_at": event.recorded_at.isoformat(),
        "type": t,
        "summary": summaries.get(t, f"{t} {p}"),
    }
