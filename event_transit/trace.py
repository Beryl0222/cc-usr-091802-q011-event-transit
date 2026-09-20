"""赛后追溯：拥堵告警因果链，以及按原始时区重放任意时刻。

所有结论都从只增事件日志重建，不依赖任何易失内存状态：

* :meth:`trace_alert` —— 选任一拥堵告警，沿
  「发令批次 → 选手（号码布/核销）→ 车辆/班次 → 路段封控 → 处置决定 → 站点通知回执」
  还原来龙去脉；
* :meth:`replay_at` —— 按 Asia/Shanghai 重放某时刻调度员能看到的
  可用运力、免费乘车核销、未送达通知。
"""

from __future__ import annotations

from typing import Any

from .clock import format_dt, parse
from .dispatch import DispatchService
from .entitlements import EntitlementService
from .errors import NotFound
from .events import EventLog
from .model import State


class TraceService:
    def __init__(self, log: EventLog):
        self.log = log
        self.entitlements = EntitlementService(log)
        self.dispatch = DispatchService(log)

    # ============================================================ 告警溯源

    def trace_alert(self, alert_id: str) -> dict[str, Any]:
        events = self.log.all()
        alert_events = [e for e in events
                        if e.type == "congestion_alerted" and e.data["alert_id"] == alert_id]
        if not alert_events:
            raise NotFound(f"告警 {alert_id} 不存在", "alert_unknown")
        alert_ev = alert_events[0]
        segment_id = alert_ev.data["segment_id"]

        st = State.fold(events)
        segment = st.segments.get(segment_id, {"segment_id": segment_id})

        # 1) 同一路段上的封控及其处置
        closures = [c for c in st.closures.values()
                    if segment_id in c["segment_ids"]]
        closure_ids = {c["closure_id"] for c in closures}

        # 2) 处置决定（告警事件 refs 自身；决定可能引用班次/封控/通知）
        handlings = [e for e in events
                     if e.type == "alert_handled" and e.data["alert_id"] == alert_id]

        # 3) 受封控影响而调整的班次（含处置中直接点名的班次）
        adjusted_trip_ids = {e.data["trip_id"] for e in events
                             if e.type == "trip_adjusted"
                             and e.data.get("closure_id") in closure_ids}
        adjusted_trip_ids |= {r for h in handlings for r in h.refs
                              if r in st.trips}

        # 4) 这些班次的车辆、登车核销 → 选手 → 发令批次
        boarding = [e for e in events
                    if e.type == "boarding_recorded"
                    and e.data["trip_id"] in adjusted_trip_ids]
        ride_ids = {e.data["ride_id"] for e in boarding}
        rides = [e for e in events
                 if e.type == "ride_validated" and e.data["ride_id"] in ride_ids]
        wave_ids = {e.data.get("wave_id") for e in rides if e.data.get("wave_id")}
        reg_ids = {e.data.get("registration_id") for e in rides
                   if e.data.get("registration_id")}

        # 5) 站点通知与送达回执
        note_ids = {e.data["notification_id"] for e in events
                    if e.type == "notification_issued"
                    and (e.data.get("closure_id") in closure_ids
                         or e.data.get("trip_id") in adjusted_trip_ids)}

        # ------------------------------------------------------------ 组装
        trips_out = []
        for trip_id in sorted(adjusted_trip_ids):
            trip = st.trips[trip_id]
            related_boardings = [b for b in boarding
                                 if b.data["trip_id"] == trip_id]
            related_notes = sorted(note_ids)
            onboard = []
            for b_ev in related_boardings:
                ride_ev = next((r for r in rides
                                if r.data["ride_id"] == b_ev.data["ride_id"]), None)
                onboard.append({
                    "ride_id": b_ev.data["ride_id"],
                    "seats": b_ev.data.get("seats", 1),
                    "boarded_at": b_ev.occurred_at,
                    "bib": ride_ev.data["bib"] if ride_ev else None,
                    "registration_id": ride_ev.data.get("registration_id") if ride_ev else None,
                    "wave_id": ride_ev.data.get("wave_id") if ride_ev else None,
                    "validated_at": ride_ev.occurred_at if ride_ev else None,
                    "validated_online": ride_ev.data.get("online", True) if ride_ev else None,
                })
            trips_out.append({
                "trip_id": trip_id,
                "route_id": trip["route_id"],
                "vehicle_id": trip["vehicle_id"],
                "vehicle": st.vehicles.get(trip["vehicle_id"]),
                "scheduled_departure": format_dt(trip["scheduled_departure"]),
                "status_at_alert": _status_at(trip, parse(alert_ev.occurred_at)),
                "final_status": trip["status"],
                "adjustments": [
                    {"action": a["action"], "at": format_dt(a["at"]),
                     "closure_id": a.get("closure_id"),
                     "blocked_segments": a.get("blocked_segments", []),
                     "alternative_segments": a.get("path_override") or [],
                     "reason": a.get("reason", "")}
                    for a in trip["adjustments"]
                ],
                "onboard_validations": onboard,
                "notifications": related_notes,
            })

        runners_out = []
        for reg_id in sorted(reg_ids):
            reg = st.registrations.get(reg_id, {})
            bib = reg.get("bib")
            runners_out.append({
                "registration_id": reg_id,
                "bib": bib,
                "wave_id_at_alert": st.wave_of(reg_id, parse(alert_ev.occurred_at)),
                "withdrawn": bool(reg.get("withdrawn_at")),
            })

        notifications_out = []
        for nid in sorted(note_ids):
            n = st.notifications[nid]
            notifications_out.append({
                "notification_id": nid, "stop_id": n["stop_id"],
                "trip_id": n.get("trip_id"), "action": n.get("action"),
                "summary": n["summary"],
                "alternative_segments": n.get("alternative_segments", []),
                "issued_at": format_dt(n["issued_at"]),
                "receipts": [{"at": format_dt(r["at"]), "delivered": r["delivered"],
                              "channel": r.get("channel", ""), "detail": r.get("detail", "")}
                             for r in n["receipts"]],
                "delivered": any(r["delivered"] for r in n["receipts"]),
            })

        timeline = self._timeline(
            alert_ev, handlings, closures, trips_out, notifications_out, rides)

        return {
            "alert_id": alert_id,
            "alert": {
                "alert_id": alert_id,
                "segment_id": segment_id,
                "segment": {"from_stop": segment.get("from_stop"),
                            "to_stop": segment.get("to_stop"),
                            "name": segment.get("name", segment_id)},
                "severity": alert_ev.data["severity"],
                "note": alert_ev.data.get("note", ""),
                "source": alert_ev.data.get("source", ""),
                "observed_at": alert_ev.occurred_at,
            },
            "waves": [{"wave_id": w, **st.waves[w]} for w in sorted(wave_ids)
                      if w in st.waves],
            "runners": runners_out,
            "closures": [{"closure_id": c["closure_id"],
                          "segment_ids": c["segment_ids"], "reason": c["reason"],
                          "closed_at": format_dt(c["closed_at"]),
                          "reopened_at": format_dt(c["reopened_at"])
                          if c["reopened_at"] else None}
                         for c in closures],
            "trips": trips_out,
            "decisions": [{"at": h.occurred_at, "decision": h.data["decision"],
                           "note": h.data.get("note", ""), "actor": h.actor,
                           "refs": list(h.refs)} for h in
                          sorted(handlings, key=lambda e: e.seq)],
            "notifications": notifications_out,
            "timeline": timeline,
        }

    def _timeline(self, alert_ev, handlings, closures, trips_out,
                  notifications_out, rides) -> list[dict]:
        items: list[dict] = []
        items.append({"at": alert_ev.occurred_at, "kind": "alert",
                      "summary": f"拥堵告警 {alert_ev.data['alert_id']}："
                                 f"{alert_ev.data['severity']}（{alert_ev.data['segment_id']}）"})
        for c in closures:
            items.append({"at": format_dt(c["closed_at"]), "kind": "closure",
                          "summary": f"封控 {c['closure_id']} 封闭 {c['segment_ids']}"
                                     + (f"，{c['reason']}" if c["reason"] else "")})
            if c["reopened_at"]:
                items.append({"at": format_dt(c["reopened_at"]), "kind": "reopen",
                              "summary": f"封控 {c['closure_id']} 解除"})
        for t in trips_out:
            for a in t["adjustments"]:
                items.append({
                    "at": a["at"], "kind": "adjustment",
                    "summary": f"班次 {t['trip_id']}（车 {t['vehicle_id']}）"
                               + ("改走 " + "/".join(a["alternative_segments"])
                                  if a["action"] == "rerouted"
                                  else "因无替代路径取消")})
        for h in handlings:
            items.append({"at": h.occurred_at, "kind": "decision",
                          "summary": f"处置决定：{h.data['decision']}"
                                     + (f"（{h.data['note']}）" if h.data.get("note") else "")})
        for n in notifications_out:
            items.append({"at": n["issued_at"], "kind": "notification",
                          "summary": f"站点 {n['stop_id']} 通知：{n['summary']}"})
            for r in n["receipts"]:
                items.append({"at": r["at"], "kind": "receipt",
                              "summary": f"站点 {n['stop_id']} 回执："
                                         + ("已送达" if r["delivered"] else "未送达")})
        items.sort(key=lambda x: (x["at"], x["kind"]))
        return items

    # ============================================================ 时刻重放

    def replay_at(self, at: str, *, axis: str = "occurred") -> dict[str, Any]:
        """按赛事本地时区重放 ``at`` 时刻的世界。

        ``axis="occurred"``（默认）：按业务发生时刻——道路封控、离线验票
        即便补传更晚也计入，回答"那一刻真实的可用运力与核销"；
        ``axis="recorded"``：按平台登记时刻——回答"调度员当时屏幕上看到的"。
        """
        t = parse(at)
        fleet = self.dispatch.fleet_snapshot(t, axis=axis)
        st = State.fold(self.log.all(), t, axis=axis)
        # 核销按所选时间轴过滤：登记轴下，尚未补传的离线记录不可见
        rows = []
        for e in self.log.by_type("ride_validated"):
            ts = e.occurred_at if axis == "occurred" else e.recorded_at
            if parse(ts) > t:
                continue
            d = e.data
            rows.append({
                "ride_id": d["ride_id"], "at": e.occurred_at,
                "recorded_at": e.recorded_at,
                "device_id": d["device_id"], "bib": d["bib"], "mode": d["mode"],
                "decision": d["decision"], "online": d.get("online", True),
                "window_id": d.get("window_id"), "wave_id": d.get("wave_id"),
            })
        rows.sort(key=lambda r: r["at"])
        undelivered = [
            {"notification_id": n["notification_id"], "stop_id": n["stop_id"],
             "trip_id": n.get("trip_id"), "summary": n["summary"],
             "issued_at": format_dt(n["issued_at"]),
             "channels_without_receipt": _missing_channels(n)}
            for n in st.undelivered(t)
        ]
        waves_now = []
        for w in st.waves.values():
            waves_now.append({"wave_id": w["wave_id"], "name": w["name"],
                              "start": w["start"],
                              "fired": parse(w["start"]) <= t})
        return {
            "tz": "Asia/Shanghai",
            "at": format_dt(t),
            "axis": axis,
            "waves": waves_now,
            "capacity": {
                "shuttle_available": fleet["shuttle"]["available"],
                "medical_available": fleet["medical"]["available"],
                "closed_segments": fleet["closed_segments"],
                "green_hospitals": fleet["green_hospitals"],
                "trips": fleet["shuttle"]["trips"] + fleet["medical"]["trips"],
            },
            "ride_redemptions": {
                "total": len(rows),
                "free_rides": sum(1 for r in rows if r["decision"] == "allow_free"),
                "denied": sum(1 for r in rows if r["decision"] == "deny"),
                "offline_synced": sum(1 for r in rows if not r["online"]),
                "rows": rows,
            },
            "undelivered_notifications": undelivered,
        }

    def replay_timeline(self, points: list[str]) -> list[dict]:
        """沿一串时刻逐点重放，便于回放运力/核销/通知如何演变。"""
        return [self.replay_at(p) for p in sorted(parse(p) for p in points)]


def _status_at(trip: dict, at) -> str:
    if trip["departed_at"] and trip["departed_at"] <= at:
        return "departed"
    adjustments_before = [a for a in trip["adjustments"] if a["at"] <= at]
    if any(a["action"] == "cancelled" for a in adjustments_before):
        return "cancelled"
    return "scheduled"


def _missing_channels(notification: dict) -> list[str]:
    acked = {r.get("channel") for r in notification["receipts"] if r.get("delivered")}
    channels = {notification.get("channel", "station_display")}
    return sorted(channels - acked)
