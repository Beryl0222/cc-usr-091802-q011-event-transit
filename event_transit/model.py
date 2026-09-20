"""把只增事件日志折叠成某一时刻的世界状态。

``State.at(log, t)`` 给出 **业务发生时间** 不晚于 ``t`` 的全部事件折叠结果
（含离线补传的历史事件）——用于按真实发生时刻判断凭证效力与容量；
``State.at(log, t, axis="recorded")`` 只统计登记时间不晚于 ``t`` 的事件——
用于复盘"调度员当时屏幕上实际看到的东西"。

挂失 / 退赛 / 改枪只是在事件发生时刻之后改变折叠结果；更早的通行记录
（``ride_validated`` 中冻结的 ``basis``）原样保留，永不回改。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from .clock import parse

# ---------------------------------------------------------------------------
# 事件数据契约见 README「事件类型」。fold 只按 occurred 时间重放。
# ---------------------------------------------------------------------------


class State:
    """某一时刻的只读状态快照（由事件折叠得到，字段均为普通字典/列表）。"""

    def __init__(self) -> None:
        self.policy: dict[str, Any] = {"medical_isolation": True, "freeze_minutes": 15}
        self.waves: dict[str, dict] = {}
        self.windows: dict[str, dict] = {}
        self.stops: dict[str, dict] = {}
        self.segments: dict[str, dict] = {}
        self.routes: dict[str, dict] = {}
        self.vehicles: dict[str, dict] = {}
        self.hospitals: dict[str, dict] = {}
        self.devices: dict[str, dict] = {}

        self.registrations: dict[str, dict] = {}
        self.bibs: dict[str, dict] = {}
        self.trips: dict[str, dict] = {}
        self.closures: dict[str, dict] = {}
        self.rides: dict[str, dict] = {}
        self.notifications: dict[str, dict] = {}
        self.alerts: dict[str, dict] = {}

    # ================================================================ 折叠

    @classmethod
    def fold(cls, events, at=None, axis: str = "occurred") -> "State":
        state = cls()
        cutoff = parse(at) if at is not None else None
        chosen = []
        for e in events:
            ts = e.occurred_at if axis == "occurred" else e.recorded_at
            if cutoff is not None and parse(ts) > cutoff:
                continue
            chosen.append(e)
        chosen.sort(key=lambda e: (parse(e.occurred_at if axis == "occurred" else e.recorded_at), e.seq))
        for e in chosen:
            state.apply(e.type, e.data, parse(e.occurred_at))
        return state

    def apply(self, etype: str, d: dict, at) -> None:
        handler = getattr(self, f"_on_{etype}", None)
        if handler:
            handler(d, at)

    # ------------------------------------------------------------ 基础数据

    def _on_policy_configured(self, d, at):
        self.policy.update(d)

    def _on_wave_defined(self, d, at):
        self.waves[d["wave_id"]] = {**d}

    def _on_window_defined(self, d, at):
        self.windows[d["window_id"]] = {**d}

    def _on_stop_defined(self, d, at):
        self.stops[d["stop_id"]] = {**d}

    def _on_segment_defined(self, d, at):
        self.segments[d["segment_id"]] = {**d}

    def _on_route_defined(self, d, at):
        self.routes[d["route_id"]] = {**d}

    def _on_vehicle_defined(self, d, at):
        self.vehicles[d["vehicle_id"]] = {**d}

    def _on_hospital_defined(self, d, at):
        self.hospitals[d["hospital_id"]] = {"green_open": False, **d}

    def _on_green_channel_set(self, d, at):
        if d["hospital_id"] in self.hospitals:
            self.hospitals[d["hospital_id"]]["green_open"] = bool(d["open"])

    def _on_device_registered(self, d, at):
        self.devices[d["device_id"]] = {**d}

    # ------------------------------------------------------------ 报名/号码布

    def _on_runner_registered(self, d, at):
        self.registrations[d["registration_id"]] = {
            "registration_id": d["registration_id"],
            "wave_history": [(at, d.get("wave_id"))],
            "withdrawn_at": None,
            "bib": None,
        }

    def _on_bib_issued(self, d, at):
        bib = d["bib"]
        reg_id = d["registration_id"]
        self.bibs[bib] = {
            "bib": bib,
            "registration_id": reg_id,
            "issued_at": at,
            "lost_at": None,
            "replaced_by": None,
        }
        if reg_id in self.registrations:
            self.registrations[reg_id]["bib"] = bib

    def _on_bib_lost(self, d, at):
        bib = self.bibs.get(d["bib"])
        if bib and bib["lost_at"] is None:
            bib["lost_at"] = at

    def _on_bib_replaced(self, d, at):
        old = self.bibs.get(d["old_bib"])
        if old:
            old["replaced_by"] = d["new_bib"]
            old["lost_at"] = old["lost_at"] or at
        reg_id = old["registration_id"] if old else d.get("registration_id")
        self.bibs[d["new_bib"]] = {
            "bib": d["new_bib"],
            "registration_id": reg_id,
            "issued_at": at,
            "lost_at": None,
            "replaced_by": None,
        }
        if reg_id and reg_id in self.registrations:
            self.registrations[reg_id]["bib"] = d["new_bib"]

    def _on_registration_withdrawn(self, d, at):
        reg = self.registrations.get(d["registration_id"])
        if reg and reg["withdrawn_at"] is None:
            reg["withdrawn_at"] = at

    def _on_wave_changed(self, d, at):
        reg = self.registrations.get(d["registration_id"])
        if reg:
            reg["wave_history"].append((at, d["to_wave"]))

    # ------------------------------------------------------------ 班次/运力

    def _on_trip_scheduled(self, d, at):
        self.trips[d["trip_id"]] = {
            "trip_id": d["trip_id"],
            "route_id": d["route_id"],
            "vehicle_id": d["vehicle_id"],
            "scheduled_departure": parse(d["scheduled_departure"]),
            "status": "scheduled",
            "adjustments": [],
            "path_override": None,
            "departed_at": None,
            "claims": [],          # (ride_id/case_ref, seats, at, kind)
            "reservations": [],    # (case_ref, seats) —— 预留即锁定座位
            "transported": [],     # case_ref —— 实际转运（不重复扣容量）
        }

    def _on_medical_reserved(self, d, at):
        trip = self.trips.get(d["trip_id"])
        if trip:
            trip["reservations"].append((d["case_ref"], int(d["seats"])))

    def _on_road_closed(self, d, at):
        self.closures[d["closure_id"]] = {
            "closure_id": d["closure_id"],
            "segment_ids": list(d["segment_ids"]),
            "reason": d.get("reason", ""),
            "detours": d.get("detours", {}),
            "closed_at": at,
            "reopened_at": None,
        }

    def _on_road_reopened(self, d, at):
        closure = self.closures.get(d["closure_id"])
        if closure and closure["reopened_at"] is None:
            closure["reopened_at"] = at

    def _on_trip_adjusted(self, d, at):
        trip = self.trips.get(d["trip_id"])
        if not trip:
            return
        trip["adjustments"].append({**d, "at": at})
        if d["action"] == "cancelled":
            trip["status"] = "cancelled"
        elif d["action"] == "rerouted":
            trip["path_override"] = d.get("path_override")
        elif d["action"] == "restored":
            # 封控解除、班次仍未发车：撤销绕行，回到原线路
            trip["path_override"] = None

    def _on_trip_departed(self, d, at):
        trip = self.trips.get(d["trip_id"])
        if trip:
            trip["status"] = "departed"
            trip["departed_at"] = at

    def _on_trip_completed(self, d, at):
        trip = self.trips.get(d["trip_id"])
        if trip:
            trip["status"] = "completed"

    def _on_boarding_recorded(self, d, at):
        trip = self.trips.get(d["trip_id"])
        if trip:
            trip["claims"].append((d["ride_id"], int(d.get("seats", 1)), at, "shuttle"))

    def _on_medical_transported(self, d, at):
        trip = self.trips.get(d["trip_id"])
        if trip and d["case_ref"] not in trip["transported"]:
            trip["transported"].append(d["case_ref"])

    # ------------------------------------------------------------ 验票/通知/告警

    def _on_ride_validated(self, d, at):
        self.rides[d["ride_id"]] = {**d, "at": at}

    def _on_notification_issued(self, d, at):
        self.notifications[d["notification_id"]] = {
            **d,
            "issued_at": at,
            "receipts": [],
        }

    def _on_notification_receipt(self, d, at):
        n = self.notifications.get(d["notification_id"])
        if n:
            n["receipts"].append({**d, "at": at})

    def _on_congestion_alerted(self, d, at):
        self.alerts[d["alert_id"]] = {
            **d,
            "at": at,
            "handlings": [],
        }

    def _on_alert_handled(self, d, at):
        alert = self.alerts.get(d["alert_id"])
        if alert:
            alert["handlings"].append({**d, "at": at})

    # ================================================================ 查询

    def wave_of(self, registration_id, at):
        reg = self.registrations.get(registration_id)
        if not reg:
            return None
        current = None
        for eff, wave_id in reg["wave_history"]:
            if eff <= at:
                current = wave_id
        return current

    def bib_status(self, bib: str, at) -> dict:
        """号码布在 ``at`` 时刻的效力。返回 decision/registration/wave/reasons。"""
        record = self.bibs.get(bib)
        if not record:
            return {"decision": "deny", "reasons": ["bib_unknown"], "registration_id": None, "wave_id": None}
        if record["issued_at"] > at:
            return {"decision": "deny", "reasons": ["bib_not_yet_issued"],
                    "registration_id": record["registration_id"], "wave_id": None}
        if record["replaced_by"]:
            return {"decision": "deny", "reasons": ["bib_replaced"],
                    "registration_id": record["registration_id"], "wave_id": None,
                    "replacement": record["replaced_by"]}
        if record["lost_at"] and record["lost_at"] <= at:
            return {"decision": "deny", "reasons": ["bib_reported_lost"],
                    "registration_id": record["registration_id"], "wave_id": None}
        reg = self.registrations.get(record["registration_id"])
        if not reg:
            return {"decision": "deny", "reasons": ["registration_unknown"],
                    "registration_id": record["registration_id"], "wave_id": None}
        if reg["withdrawn_at"] and reg["withdrawn_at"] <= at:
            return {"decision": "deny", "reasons": ["registration_withdrawn"],
                    "registration_id": reg["registration_id"], "wave_id": None}
        wave_id = self.wave_of(reg["registration_id"], at)
        return {"decision": "allow", "reasons": [],
                "registration_id": reg["registration_id"], "wave_id": wave_id}

    def active_window(self, mode: str, at, wave_id: str | None = None):
        for w in self.windows.values():
            if w["mode"] != mode:
                continue
            if not (parse(w["start"]) <= at < parse(w["end"])):
                continue
            if wave_id is not None and w.get("waves") and wave_id not in w["waves"]:
                continue
            return w
        return None

    def closure_active(self, closure: dict, at) -> bool:
        return closure["closed_at"] <= at and (
            closure["reopened_at"] is None or closure["reopened_at"] > at
        )

    def closed_segments(self, at) -> set[str]:
        out = set()
        for c in self.closures.values():
            if self.closure_active(c, at):
                out.update(c["segment_ids"])
        return out

    def segment_open(self, segment_id: str, at) -> bool:
        return segment_id not in self.closed_segments(at)

    def route_path(self, trip: dict, at) -> list[str]:
        """班次当前实际行驶的路段序列（封控改派后为绕行路径）。"""
        if trip.get("path_override"):
            return list(trip["path_override"])
        route = self.routes.get(trip["route_id"])
        return list(route["segment_ids"]) if route else []

    def trip_capacity(self, trip: dict, at) -> dict:
        vehicle = self.vehicles.get(trip["vehicle_id"], {})
        cap = int(vehicle.get("capacity", 0))
        reserved = sum(s for _, s in trip["reservations"])
        claimed = sum(s for _, s, t, _ in trip["claims"] if t <= at)
        return {"capacity": cap, "reserved_medical": reserved, "claimed": claimed,
                "available": max(0, cap - reserved - claimed),
                "oversold": max(0, reserved + claimed - cap)}

    def open_hospitals(self) -> list[dict]:
        return [h for h in self.hospitals.values() if h.get("green_open")]

    def undelivered(self, at) -> list[dict]:
        """已发出、截至 ``at`` 仍无成功送达回执的通知。"""
        out = []
        for n in self.notifications.values():
            if n["issued_at"] > at:
                continue
            delivered = any(r.get("delivered") and r["at"] <= at for r in n["receipts"])
            if not delivered:
                out.append(n)
        return out
