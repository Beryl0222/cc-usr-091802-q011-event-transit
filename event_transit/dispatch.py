"""线路班次、车辆容量、道路管制、医疗转运与站点通知。

关键不变量：

1. **容量占用只增且幂等**：每次登车/医疗占位都有唯一引用（``ride_id`` /
   ``case_ref``），重放同一记录命中去重分支，座位绝不二次占用。
2. **普通接驳与医疗转运物理隔离**：接驳车（``kind="shuttle"``）拒绝医疗
   占位，医疗车（``kind="medical"``）拒绝普通登车；医疗预留座位对
   普通接驳始终不可见、不可挤占。
3. **封控只改未发车班次**：以封控发生时刻为准，已发车班次不动；
   未发车班次在站点图上重算绕行路径，无法绕行才取消，并向受影响站点
   发出替代路线通知，通知送达以回执为准（重放时可统计未送达）。
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Any

from .clock import format_dt, now, parse
from .errors import Conflict, DuplicateReplay, NotFound, TransitError
from .events import EventLog
from .model import State


class DispatchService:
    def __init__(self, log: EventLog):
        self.log = log
        self._lock = threading.RLock()

    # ============================================================ 路网基础

    def define_stop(self, stop_id: str, name: str, *, occurred_at=None, actor="planner"):
        with self._lock:
            return self.log.append("stop_defined", {"stop_id": stop_id, "name": name},
                                   occurred_at=occurred_at, actor=actor, refs=(stop_id,))

    def define_segment(self, segment_id: str, from_stop: str, to_stop: str,
                       name: str = "", *, occurred_at=None, actor="planner"):
        with self._lock:
            return self.log.append("segment_defined", {
                "segment_id": segment_id, "from_stop": from_stop,
                "to_stop": to_stop, "name": name or segment_id,
            }, occurred_at=occurred_at, actor=actor, refs=(segment_id,))

    def define_route(self, route_id: str, name: str, stop_ids: list[str],
                     segment_ids: list[str], *, kind: str = "shuttle",
                     occurred_at=None, actor="planner"):
        """``segment_ids`` 须与相邻站点一一对应：第 i 段连接 stop_ids[i]→[i+1]。"""
        if len(segment_ids) != len(stop_ids) - 1:
            raise TransitError("路段数必须等于站点数减一", "bad_route_geometry")
        if kind not in ("shuttle", "medical"):
            raise TransitError("线路类型只能是 shuttle 或 medical", "bad_route_kind")
        with self._lock:
            return self.log.append("route_defined", {
                "route_id": route_id, "name": name, "stop_ids": list(stop_ids),
                "segment_ids": list(segment_ids), "kind": kind,
            }, occurred_at=occurred_at, actor=actor, refs=(route_id,))

    def define_vehicle(self, vehicle_id: str, plate: str, capacity: int,
                       kind: str = "shuttle", *, occurred_at=None, actor="planner"):
        if capacity <= 0:
            raise TransitError("车辆容量必须为正", "bad_capacity")
        if kind not in ("shuttle", "medical"):
            raise TransitError("车辆类型只能是 shuttle 或 medical", "bad_vehicle_kind")
        with self._lock:
            return self.log.append("vehicle_defined", {
                "vehicle_id": vehicle_id, "plate": plate,
                "capacity": int(capacity), "kind": kind,
            }, occurred_at=occurred_at, actor=actor, refs=(vehicle_id,))

    def define_hospital(self, hospital_id: str, name: str, stop_id: str,
                        *, occurred_at=None, actor="planner"):
        with self._lock:
            return self.log.append("hospital_defined", {
                "hospital_id": hospital_id, "name": name, "stop_id": stop_id,
            }, occurred_at=occurred_at, actor=actor, refs=(hospital_id,))

    def set_green_channel(self, hospital_id: str, open_: bool, *,
                          occurred_at=None, actor="medical"):
        with self._lock:
            st = State.fold(self.log.all())
            if hospital_id not in st.hospitals:
                raise NotFound(f"医院 {hospital_id} 不存在", "hospital_unknown")
            return self.log.append("green_channel_set", {
                "hospital_id": hospital_id, "open": bool(open_),
            }, occurred_at=occurred_at, actor=actor, refs=(hospital_id,))

    # ============================================================ 班次

    def schedule_trip(self, trip_id: str, route_id: str, vehicle_id: str,
                      scheduled_departure: str, *, occurred_at=None, actor="planner"):
        with self._lock:
            st = State.fold(self.log.all())
            if trip_id in st.trips:
                raise Conflict(f"班次 {trip_id} 已存在", "trip_exists")
            route = st.routes.get(route_id)
            vehicle = st.vehicles.get(vehicle_id)
            if not route:
                raise NotFound(f"线路 {route_id} 不存在", "route_unknown")
            if not vehicle:
                raise NotFound(f"车辆 {vehicle_id} 不存在", "vehicle_unknown")
            if route["kind"] != vehicle["kind"]:
                raise TransitError("线路与车辆类型不匹配", "route_vehicle_kind_mismatch")
            return self.log.append("trip_scheduled", {
                "trip_id": trip_id, "route_id": route_id, "vehicle_id": vehicle_id,
                "scheduled_departure": format_dt(parse(scheduled_departure)),
            }, occurred_at=occurred_at or scheduled_departure, actor=actor,
                refs=(trip_id, route_id, vehicle_id))

    def trip_status(self, trip_id: str, at=None) -> dict:
        at = parse(at) if at else now()
        st = State.fold(self.log.all(), at)
        trip = st.trips.get(trip_id)
        if not trip:
            raise NotFound(f"班次 {trip_id} 不存在", "trip_unknown")
        cap = st.trip_capacity(trip, at)
        return {"trip_id": trip_id, "route_id": trip["route_id"],
                "vehicle_id": trip["vehicle_id"], "status": trip["status"],
                "scheduled_departure": format_dt(trip["scheduled_departure"]),
                "departed_at": format_dt(trip["departed_at"]) if trip["departed_at"] else None,
                "path": st.route_path(trip, at), **cap,
                "medical_reserved": [{"case_ref": r, "seats": s}
                                     for r, s in trip["reservations"]],
                "medical_transported": list(trip["transported"]),
                "adjustments": [a["action"] for a in trip["adjustments"]]}

    def depart_trip(self, trip_id: str, *, occurred_at=None, actor="driver"):
        """发车；发车时刻仍被封控路段阻挡的班次不得发车。"""
        at = parse(occurred_at) if occurred_at else now()
        with self._lock:
            st = State.fold(self.log.all(), at)
            trip = st.trips.get(trip_id)
            if not trip:
                raise NotFound(f"班次 {trip_id} 不存在", "trip_unknown")
            if trip["status"] in ("departed", "completed"):
                raise Conflict(f"班次 {trip_id} 已发车", "trip_already_departed")
            if trip["status"] == "cancelled":
                raise Conflict(f"班次 {trip_id} 已取消，不能发车", "trip_cancelled")
            blocked = [seg for seg in st.route_path(trip, at)
                       if not st.segment_open(seg, at)]
            if blocked:
                raise Conflict(f"行驶路径仍被封控路段阻挡：{blocked}",
                               "route_blocked", {"segments": blocked})
            return self.log.append("trip_departed", {"trip_id": trip_id},
                                   occurred_at=at, actor=actor,
                                   refs=(trip_id, trip["route_id"], trip["vehicle_id"]))

    def complete_trip(self, trip_id: str, *, occurred_at=None, actor="driver"):
        at = parse(occurred_at) if occurred_at else now()
        with self._lock:
            if trip_id not in State.fold(self.log.all(), at).trips:
                raise NotFound(f"班次 {trip_id} 不存在", "trip_unknown")
            return self.log.append("trip_completed", {"trip_id": trip_id},
                                   occurred_at=at, actor=actor, refs=(trip_id,))

    # ============================================================ 容量占用

    def _capacity_at(self, st: State, trip: dict, at) -> dict:
        return st.trip_capacity(trip, at)

    def board_shuttle(self, trip_id: str, ride_id: str, seats: int = 1, *,
                      occurred_at=None, actor="onboard"):
        """普通接驳登车。

        必须是接驳车、班次未取消；医疗车直接拒绝（医疗资源隔离）。
        ``ride_id`` 去重键保证同一核销记录重放不重复占座。
        """
        at = parse(occurred_at) if occurred_at else now()
        seats = int(seats)
        if seats <= 0:
            raise TransitError("登车座位数必须为正", "bad_seats")
        with self._lock:
            dedupe_key = f"board:{trip_id}:{ride_id}"
            original = self.log.duplicate_of(dedupe_key)
            if original is not None:
                # 容量满员后重放也必须是"重复"而不是"容量不足"
                raise DuplicateReplay(
                    f"登车记录 {ride_id} 已入账（seq={original.seq}）",
                    details={"event": original.to_dict()})
            st = State.fold(self.log.all(), at)
            trip = st.trips.get(trip_id)
            if not trip:
                raise NotFound(f"班次 {trip_id} 不存在", "trip_unknown")
            vehicle = st.vehicles[trip["vehicle_id"]]
            if vehicle["kind"] != "shuttle":
                raise TransitError("普通接驳不得占用医疗转运车辆",
                                   "medical_resource_protected")
            if trip["status"] == "cancelled":
                raise Conflict("班次已取消", "trip_cancelled")
            cap = self._capacity_at(st, trip, at)
            if cap["available"] < seats:
                raise Conflict(
                    f"班次 {trip_id} 容量不足：需 {seats}，余 {cap['available']}",
                    "capacity_exceeded", {"available": cap["available"]})
            event = self.log.append("boarding_recorded", {
                "trip_id": trip_id, "ride_id": ride_id, "seats": seats,
            }, occurred_at=at, actor=actor,
                dedupe_key=dedupe_key,
                refs=(trip_id, ride_id, trip["vehicle_id"], trip["route_id"]))
            return {"status": "boarded", "event": event.to_dict(),
                    "remaining": cap["available"] - seats}

    def reserve_medical(self, trip_id: str, case_ref: str, seats: int,
                        hospital_id: str, *, occurred_at=None, actor="medical"):
        """为急救病例预留医疗车座位（计入医疗专用容量，普通接驳不可见）。"""
        at = parse(occurred_at) if occurred_at else now()
        seats = int(seats)
        if seats <= 0:
            raise TransitError("预留座位数必须为正", "bad_seats")
        with self._lock:
            dedupe_key = f"medres:{trip_id}:{case_ref}"
            original = self.log.duplicate_of(dedupe_key)
            if original is not None:
                raise DuplicateReplay(
                    f"病例 {case_ref} 已在班次 {trip_id} 预留（seq={original.seq}）",
                    details={"event": original.to_dict()})
            st = State.fold(self.log.all(), at)
            trip = st.trips.get(trip_id)
            if not trip:
                raise NotFound(f"班次 {trip_id} 不存在", "trip_unknown")
            vehicle = st.vehicles[trip["vehicle_id"]]
            if vehicle["kind"] != "medical":
                raise TransitError("医疗转运只能使用医疗车辆",
                                    "shuttle_cannot_carry_medical")
            hospital = st.hospitals.get(hospital_id)
            if not hospital:
                raise NotFound(f"医院 {hospital_id} 不存在", "hospital_unknown")
            if not hospital["green_open"]:
                raise Conflict(f"医院 {hospital_id} 绿色通道未开启",
                               "green_channel_closed")
            blocked = [seg for seg in st.route_path(trip, at)
                       if not st.segment_open(seg, at)]
            if blocked:
                raise Conflict("医疗班次路径被封控阻挡，请改派",
                               "route_blocked", {"segments": blocked})
            cap = self._capacity_at(st, trip, at)
            if cap["available"] < seats:
                raise Conflict(
                    f"医疗班次 {trip_id} 容量不足：需 {seats}，余 {cap['available']}",
                    "medical_capacity_exceeded", {"available": cap["available"]})
            event = self.log.append("medical_reserved", {
                "trip_id": trip_id, "case_ref": case_ref,
                "seats": seats, "hospital_id": hospital_id,
            }, occurred_at=at, actor=actor,
                dedupe_key=dedupe_key,
                refs=(trip_id, case_ref, hospital_id, trip["vehicle_id"]))
            return {"status": "reserved", "event": event.to_dict(),
                    "medical_remaining": cap["available"] - seats}

    def transport_medical(self, trip_id: str, case_ref: str, *,
                          occurred_at=None, actor="medical"):
        """记录伤员实际转运上车；病例必须已在该班次预留。"""
        at = parse(occurred_at) if occurred_at else now()
        with self._lock:
            st = State.fold(self.log.all(), at)
            trip = st.trips.get(trip_id)
            if not trip:
                raise NotFound(f"班次 {trip_id} 不存在", "trip_unknown")
            reserved = {ref: seats for ref, seats in trip["reservations"]}
            if case_ref not in reserved:
                raise NotFound(f"病例 {case_ref} 未在该班次预留", "case_not_reserved")
            event = self.log.append("medical_transported", {
                "trip_id": trip_id, "case_ref": case_ref,
                "seats": reserved[case_ref],
            }, occurred_at=at, actor=actor,
                dedupe_key=f"medtr:{trip_id}:{case_ref}",
                refs=(trip_id, case_ref, trip["vehicle_id"]))
            return {"status": "transported", "event": event.to_dict()}

    # ============================================================ 道路封控与改派

    def _find_path(self, st: State, start: str, goal: str,
                   blocked: set[str]) -> list[str] | None:
        """在站点图上 BFS：返回避开封控路段的替代路段序列，无路返回 None。"""
        adjacency: dict[str, list[tuple[str, str]]] = {}
        for seg_id, seg in st.segments.items():
            if seg_id in blocked:
                continue
            adjacency.setdefault(seg["from_stop"], []).append((seg["to_stop"], seg_id))
        queue = deque([(start, [])])
        seen = {start}
        while queue:
            stop, edges = queue.popleft()
            if stop == goal:
                return edges
            for nxt, edge in adjacency.get(stop, []):
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append((nxt, edges + [edge]))
        return None

    def close_road(self, closure_id: str, segment_ids: list[str], *,
                   reason: str = "", occurred_at=None, actor="police") -> dict:
        """道路封闭 → 只调整尚未发车的班次 → 重算绕行 → 通知受影响站点。

        已发车班次即使路径经过封控路段也**不动**（司机按现场处置，
        其状态由另外的现场事件记录）；通知一律带替代路线并等待回执。
        """
        at = parse(occurred_at) if occurred_at else now()
        with self._lock:
            st = State.fold(self.log.all(), at)
            if closure_id in st.closures:
                raise Conflict(f"封控 {closure_id} 已存在", "closure_exists")
            missing = [s for s in segment_ids if s not in st.segments]
            if missing:
                raise NotFound(f"路段不存在：{missing}", "segment_unknown")
            self.log.append("road_closed", {
                "closure_id": closure_id, "segment_ids": list(segment_ids),
                "reason": reason,
            }, occurred_at=at, actor=actor,
                refs=[closure_id, *segment_ids])
            blocked = st.closed_segments(at) | set(segment_ids)
            # 计数同时看截至时刻的状态与日志全量，避免补传历史事件时撞号
            notif_counter = max(len(st.notifications), len(self.log.by_type("notification_issued")))

            adjustments: list[dict] = []
            notifications: list[dict] = []
            for trip_id, trip in sorted(st.trips.items()):
                if trip["status"] != "scheduled":
                    continue  # 已发车/已完成/已取消：一律不调整
                route = st.routes[trip["route_id"]]
                current_path = st.route_path(trip, at)
                hit = [s for s in current_path if s in set(segment_ids)]
                if not hit:
                    continue
                # 沿原线路站点序逐跳重算绕行
                new_edges: list[str] = []
                possible = True
                for a, b in zip(route["stop_ids"], route["stop_ids"][1:]):
                    hop = self._find_path(st, a, b, blocked)
                    if hop is None:
                        possible = False
                        break
                    new_edges.extend(hop)
                affected_stops = self._affected_stops(st, route, hit)
                if possible and new_edges:
                    self.log.append("trip_adjusted", {
                        "trip_id": trip_id, "action": "rerouted",
                        "closure_id": closure_id,
                        "blocked_segments": hit,
                        "path_override": new_edges,
                    }, occurred_at=at, actor="dispatcher",
                        refs=(trip_id, closure_id, trip["route_id"], *hit))
                    adjustment = {"trip_id": trip_id, "action": "rerouted",
                                  "blocked_segments": hit, "alternative_segments": new_edges}
                else:
                    self.log.append("trip_adjusted", {
                        "trip_id": trip_id, "action": "cancelled",
                        "closure_id": closure_id,
                        "blocked_segments": hit,
                        "reason": "no_alternative_path",
                    }, occurred_at=at, actor="dispatcher",
                        refs=(trip_id, closure_id, trip["route_id"], *hit))
                    adjustment = {"trip_id": trip_id, "action": "cancelled",
                                  "blocked_segments": hit, "alternative_segments": []}
                adjustments.append(adjustment)
                for stop_id in affected_stops:
                    notif_counter += 1
                    issued = self._issue_notification(
                        st, nid=f"note-{notif_counter:05d}", stop_id=stop_id,
                        trip_id=trip_id, route_id=trip["route_id"],
                        closure_id=closure_id, adjustment=adjustment,
                        at=at, reason=reason)
                    notifications.append(issued)
            return {"closure_id": closure_id, "at": format_dt(at),
                    "segment_ids": list(segment_ids),
                    "adjustments": adjustments, "notifications": notifications}

    @staticmethod
    def _affected_stops(st: State, route: dict, hit_segments: list[str]) -> list[str]:
        """受影响站点：封控路段两端且在本线路站点序内的站点。"""
        stops = []
        for seg_id in hit_segments:
            seg = st.segments[seg_id]
            for endpoint in (seg["from_stop"], seg["to_stop"]):
                if endpoint in route["stop_ids"] and endpoint not in stops:
                    stops.append(endpoint)
        return stops

    def _issue_notification(self, st: State, *, nid, stop_id, trip_id, route_id,
                            closure_id, adjustment, at, reason) -> dict:
        alt = adjustment["alternative_segments"]
        route = st.routes[route_id]
        payload = {
            "notification_id": nid, "stop_id": stop_id, "trip_id": trip_id,
            "route_id": route_id, "closure_id": closure_id,
            "channel": "station_display",
            "summary": (f"【{route['name']}】班次 {trip_id} 因{reason or '道路管制'}"
                        + (f"改走 {'/'.join(alt)}，请按站内引导候车"
                           if adjustment["action"] == "rerouted"
                           else "暂无替代路径，班次取消，请等待后续接驳")),
            "alternative_segments": alt,
            "action": adjustment["action"],
        }
        self.log.append("notification_issued", payload, occurred_at=at,
                        actor="dispatcher",
                        refs=(nid, stop_id, trip_id, route_id, closure_id))
        return payload

    def reopen_road(self, closure_id: str, *, occurred_at=None, actor="police"):
        """解除封控；仍未发车且因该封控改绕行的班次自动恢复原线。

        已取消的班次不自动复活（是否重开由调度另行决定）；
        若原线路仍被其他生效封控阻挡，也保持绕行不动。
        """
        at = parse(occurred_at) if occurred_at else now()
        with self._lock:
            st_before = State.fold(self.log.all(), at)
            if closure_id not in st_before.closures:
                raise NotFound(f"封控 {closure_id} 不存在", "closure_unknown")
            self.log.append("road_reopened", {"closure_id": closure_id},
                            occurred_at=at, actor=actor, refs=(closure_id,))
            st = State.fold(self.log.all(), at)
            blocked = st.closed_segments(at)
            restored = []
            for trip_id, trip in sorted(st.trips.items()):
                if trip["status"] != "scheduled" or not trip.get("path_override"):
                    continue
                reroutes = [a for a in trip["adjustments"]
                            if a["action"] == "rerouted"
                            and a.get("closure_id") == closure_id]
                if not reroutes:
                    continue
                route = st.routes.get(trip["route_id"])
                if route and any(s in blocked for s in route["segment_ids"]):
                    continue  # 原线仍有别的封控
                self.log.append("trip_adjusted", {
                    "trip_id": trip_id, "action": "restored",
                    "closure_id": closure_id,
                }, occurred_at=at, actor="dispatcher",
                    refs=(trip_id, closure_id, trip["route_id"]))
                restored.append(trip_id)
            return {"closure_id": closure_id, "at": format_dt(at),
                    "restored_trips": restored}

    def record_receipt(self, notification_id: str, *, delivered: bool,
                       channel: str = "station_display", detail: str = "",
                       occurred_at=None, actor="station"):
        """站点回执：delivered=True 才算送达；断网补传同样按回执发生时刻入账。"""
        at = parse(occurred_at) if occurred_at else now()
        with self._lock:
            st = State.fold(self.log.all())
            if notification_id not in st.notifications:
                raise NotFound(f"通知 {notification_id} 不存在", "notification_unknown")
            return self.log.append("notification_receipt", {
                "notification_id": notification_id, "delivered": bool(delivered),
                "channel": channel, "detail": detail,
            }, occurred_at=at, actor=actor,
                dedupe_key=f"receipt:{notification_id}:{channel}",
                refs=(notification_id,))

    # ============================================================ 拥堵告警与处置

    def report_congestion(self, alert_id: str, segment_id: str, severity: str,
                          *, note: str = "", observed_at=None,
                          source: str = "traffic-camera", actor="ops"):
        at = parse(observed_at) if observed_at else now()
        with self._lock:
            st = State.fold(self.log.all())
            if segment_id not in st.segments:
                raise NotFound(f"路段 {segment_id} 不存在", "segment_unknown")
            if alert_id in st.alerts:
                raise Conflict(f"告警 {alert_id} 已存在", "alert_exists")
            return self.log.append("congestion_alerted", {
                "alert_id": alert_id, "segment_id": segment_id,
                "severity": severity, "note": note, "source": source,
            }, occurred_at=at, actor=actor, refs=(alert_id, segment_id))

    def handle_alert(self, alert_id: str, decision: str, *, note: str = "",
                     refs: list[str] | None = None, occurred_at=None,
                     actor="dispatcher"):
        """记录处置决定（改派/加车/通报站点…）；refs 把决定与班次/通知等串起来。"""
        at = parse(occurred_at) if occurred_at else now()
        with self._lock:
            st = State.fold(self.log.all())
            if alert_id not in st.alerts:
                raise NotFound(f"告警 {alert_id} 不存在", "alert_unknown")
            return self.log.append("alert_handled", {
                "alert_id": alert_id, "decision": decision, "note": note,
            }, occurred_at=at, actor=actor,
                refs=[alert_id, *(refs or [])])

    # ============================================================ 运力快照

    def fleet_snapshot(self, at=None, *, axis: str = "occurred") -> dict:
        """某时刻可用运力（接驳/医疗严格分列）。

        ``axis="occurred"`` 按真实发生时刻（含事后补传的历史状态）；
        ``axis="recorded"`` 按平台登记时刻（调度员当时屏幕所见）。
        """
        at = parse(at) if at else now()
        st = State.fold(self.log.all(), at, axis=axis)
        blocked = st.closed_segments(at)
        trips_out = []
        for trip_id, trip in sorted(st.trips.items()):
            if trip["scheduled_departure"] > at and trip["status"] == "scheduled":
                phase = "upcoming"
            elif trip["status"] in ("departed", "completed"):
                phase = trip["status"]
            else:
                phase = trip["status"]
            cap = st.trip_capacity(trip, at)
            path = st.route_path(trip, at)
            trips_out.append({
                "trip_id": trip_id, "kind": st.vehicles[trip["vehicle_id"]]["kind"],
                "vehicle_id": trip["vehicle_id"],
                "route_id": trip["route_id"], "phase": phase,
                "scheduled_departure": format_dt(trip["scheduled_departure"]),
                "path_blocked_now": any(s in blocked for s in path),
                "path": path, **cap,
            })

        def bucket(kind):
            rows = [t for t in trips_out if t["kind"] == kind]
            return {"trips": rows, "total_capacity": sum(t["capacity"] for t in rows),
                    "available": sum(t["available"] for t in rows
                                     if t["phase"] != "cancelled")}

        return {"at": format_dt(at), "shuttle": bucket("shuttle"),
                "medical": bucket("medical"),
                "closed_segments": sorted(blocked),
                "green_hospitals": [h["hospital_id"] for h in st.open_hospitals()]}
