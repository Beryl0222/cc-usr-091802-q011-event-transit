"""报名 / 号码布 / 发令批次 / 权益窗口，以及验票与离线补传。

核心规则：

* 凭证效力永远按 **乘坐发生时刻** 的状态判定，号码布挂失、退赛、改枪
  只影响该时刻之后的行程；``ride_validated`` 事件把当时的判定依据
  （``basis``）整段冻结，历史通行永不回改。
* 每次验票有唯一 ``ride_id``，去重键为 ``ride:<ride_id>``。同一记录
  无论在线重发还是离线补传重放，都命中幂等分支，绝不二次占用容量。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .clock import format_dt, now, parse
from .errors import Conflict, DuplicateReplay, NotFound, TransitError
from .events import EventLog
from .model import State

BENEFITS = {"free_ride", "board_only"}


@dataclass
class Verdict:
    decision: str
    reasons: list[str]
    registration_id: str | None
    wave_id: str | None
    window_id: str | None
    mode: str
    at: str
    ride_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "reasons": self.reasons,
            "registration_id": self.registration_id,
            "wave_id": self.wave_id,
            "window_id": self.window_id,
            "mode": self.mode,
            "at": self.at,
            "ride_id": self.ride_id,
        }


class EntitlementService:
    def __init__(self, log: EventLog):
        self.log = log

    # ============================================================ 基础配置

    def configure_policy(self, *, freeze_minutes: int | None = None,
                         medical_isolation: bool | None = None,
                         occurred_at=None, actor="dispatcher"):
        data: dict[str, Any] = {}
        if freeze_minutes is not None:
            data["freeze_minutes"] = int(freeze_minutes)
        if medical_isolation is not None:
            data["medical_isolation"] = bool(medical_isolation)
        return self.log.append("policy_configured", data, occurred_at=occurred_at, actor=actor)

    def define_wave(self, wave_id: str, name: str, start: str, *, color: str = "",
                    occurred_at=None, actor="planner"):
        if wave_id in self._state().waves:
            raise Conflict(f"发令批次 {wave_id} 已存在", "wave_exists")
        return self.log.append("wave_defined", {
            "wave_id": wave_id, "name": name, "start": format_dt(parse(start)),
            "color": color,
        }, occurred_at=occurred_at or start, actor=actor)

    def define_window(self, window_id: str, mode: str, start: str, end: str,
                      benefit: str = "free_ride", waves: list[str] | None = None,
                      note: str = "", occurred_at=None, actor="planner"):
        if benefit not in BENEFITS:
            raise TransitError(f"未知权益类型 {benefit}", "bad_benefit")
        st = self._state()
        if window_id in st.windows:
            raise Conflict(f"窗口 {window_id} 已存在", "window_exists")
        if parse(end) <= parse(start):
            raise TransitError("窗口结束时间必须晚于开始时间", "bad_window")
        return self.log.append("window_defined", {
            "window_id": window_id, "mode": mode,
            "start": format_dt(parse(start)), "end": format_dt(parse(end)),
            "benefit": benefit, "waves": waves or [], "note": note,
        }, occurred_at=occurred_at or start, actor=actor)

    def register_device(self, device_id: str, kind: str, location: str = "",
                        occurred_at=None, actor="ops"):
        return self.log.append("device_registered", {
            "device_id": device_id, "kind": kind, "location": location,
        }, occurred_at=occurred_at, actor=actor)

    # ============================================================ 选手生命周期

    def register_runner(self, registration_id: str, name: str, wave_id: str,
                        occurred_at=None, actor="registration"):
        st = self._state()
        if registration_id in st.registrations:
            raise Conflict(f"报名 {registration_id} 已存在", "registration_exists")
        if wave_id not in st.waves:
            raise NotFound(f"发令批次 {wave_id} 不存在", "wave_unknown")
        return self.log.append("runner_registered", {
            "registration_id": registration_id, "name": name, "wave_id": wave_id,
        }, occurred_at=occurred_at, actor=actor, refs=(registration_id, wave_id))

    def issue_bib(self, bib: str, registration_id: str, occurred_at=None, actor="expo"):
        st = self._state()
        if registration_id not in st.registrations:
            raise NotFound(f"报名 {registration_id} 不存在", "registration_unknown")
        if bib in st.bibs:
            raise Conflict(f"号码布 {bib} 已发放", "bib_exists")
        if st.registrations[registration_id]["bib"]:
            raise Conflict("该报名已持有号码布", "bib_already_held")
        wave_id = st.wave_of(registration_id, parse(occurred_at) if occurred_at else now())
        return self.log.append("bib_issued", {
            "bib": bib, "registration_id": registration_id,
        }, occurred_at=occurred_at, actor=actor,
            refs=(registration_id, bib, *( (wave_id,) if wave_id else ())))

    def report_bib_lost(self, bib: str, *, occurred_at=None, actor="runner"):
        """挂失只改变挂失时刻之后的行程，此前已冻结的核销依据保留。"""
        st = self._state()
        record = st.bibs.get(bib)
        if not record:
            raise NotFound(f"号码布 {bib} 不存在", "bib_unknown")
        if record["lost_at"]:
            raise Conflict("号码布已挂失", "bib_already_lost")
        return self.log.append("bib_lost", {"bib": bib}, occurred_at=occurred_at,
                               actor=actor, refs=(bib, record["registration_id"]))

    def replace_bib(self, old_bib: str, new_bib: str, *, occurred_at=None, actor="expo"):
        st = self._state()
        old = st.bibs.get(old_bib)
        if not old:
            raise NotFound(f"号码布 {old_bib} 不存在", "bib_unknown")
        if new_bib in st.bibs:
            raise Conflict(f"新号码布 {new_bib} 已存在", "bib_exists")
        return self.log.append("bib_replaced", {
            "old_bib": old_bib, "new_bib": new_bib,
            "registration_id": old["registration_id"],
        }, occurred_at=occurred_at, actor=actor,
            refs=(old_bib, new_bib, old["registration_id"]))

    def withdraw(self, registration_id: str, *, occurred_at=None, actor="runner"):
        st = self._state()
        reg = st.registrations.get(registration_id)
        if not reg:
            raise NotFound(f"报名 {registration_id} 不存在", "registration_unknown")
        if reg["withdrawn_at"]:
            raise Conflict("已退赛", "already_withdrawn")
        refs = [registration_id] + ([reg["bib"]] if reg["bib"] else [])
        return self.log.append("registration_withdrawn",
                               {"registration_id": registration_id},
                               occurred_at=occurred_at, actor=actor, refs=refs)

    def change_wave(self, registration_id: str, to_wave: str, *,
                    occurred_at=None, actor="dispatcher"):
        """改枪：新批次从事件发生时刻起生效；此前通行保留旧批次依据。"""
        st = self._state()
        reg = st.registrations.get(registration_id)
        if not reg:
            raise NotFound(f"报名 {registration_id} 不存在", "registration_unknown")
        if to_wave not in st.waves:
            raise NotFound(f"发令批次 {to_wave} 不存在", "wave_unknown")
        if st.wave_of(registration_id, parse(occurred_at) if occurred_at else now()) == to_wave:
            raise Conflict("选手已在该批次", "same_wave")
        refs = [registration_id, to_wave] + ([reg["bib"]] if reg["bib"] else [])
        return self.log.append("wave_changed", {
            "registration_id": registration_id, "to_wave": to_wave,
        }, occurred_at=occurred_at, actor=actor, refs=refs)

    # ============================================================ 效力查询

    def entitlement_snapshot(self, bib: str, at=None) -> dict:
        """一张号码布在指定时刻：身份/批次状态，以及各模式可用窗口。"""
        at = parse(at) if at else now()
        st = State.fold(self.log.all(), at)
        status = st.bib_status(bib, at)
        windows = []
        for w in st.windows.values():
            if status["wave_id"] and w.get("waves") and status["wave_id"] not in w["waves"]:
                continue
            windows.append({
                "window_id": w["window_id"], "mode": w["mode"], "benefit": w["benefit"],
                "start": w["start"], "end": w["end"], "note": w.get("note", ""),
                "active_now": parse(w["start"]) <= at < parse(w["end"]),
            })
        windows.sort(key=lambda x: x["start"])
        return {"at": format_dt(at), "bib": bib, **status, "windows": windows}

    def evaluate(self, device_id: str, bib: str, mode: str, at) -> tuple[State, Verdict]:
        """按乘坐发生时刻求值，不写日志。"""
        at = parse(at)
        st = State.fold(self.log.all(), at)
        if device_id not in st.devices:
            raise NotFound(f"设备 {device_id} 未登记", "device_unknown")
        status = st.bib_status(bib, at)
        window = st.active_window(mode, at, status["wave_id"])
        reasons = list(status["reasons"])
        window_id = window["window_id"] if window else None
        if status["decision"] == "allow":
            if window is None:
                verdict_decision, reasons = "deny", reasons + ["outside_entitlement_window"]
            elif window["benefit"] == "free_ride":
                verdict_decision = "allow_free"
            else:
                verdict_decision = "allow_board"
        else:
            verdict_decision = "deny"
        v = Verdict(verdict_decision, reasons, status["registration_id"],
                    status["wave_id"], window_id, mode, format_dt(at),
                    ride_id="")
        return st, v

    # ============================================================ 在线验票

    def validate_ride(self, device_id: str, bib: str, mode: str, at=None, *,
                      ride_id: str | None = None, recorded_at=None,
                      online: bool = True, bundle_id: str | None = None,
                      local_decision: str | None = None) -> dict:
        """闸机/车载设备在线验票并核销免费乘车权益。"""
        at = parse(at) if at else now()
        rid = ride_id or f"{device_id}:{bib}:{format_dt(at)}"
        try:
            _, v = self.evaluate(device_id, bib, mode, at)
        except NotFound:
            raise
        v.ride_id = rid
        event = self.log.append(
            "ride_validated",
            {
                "ride_id": rid, "device_id": device_id, "bib": bib, "mode": mode,
                "at": format_dt(at), "online": online,
                "decision": v.decision, "deny_reasons": v.reasons,
                "window_id": v.window_id,
                "registration_id": v.registration_id, "wave_id": v.wave_id,
                "bundle_id": bundle_id,
                "local_decision": local_decision,
                # 冻结依据：事后号码布即使被挂失/退赛/改枪，这条记录仍自证效力
                "basis": {**v.to_dict(), "device_id": device_id, "bib": bib,
                          "frozen": True},
            },
            occurred_at=at, recorded_at=recorded_at,
            dedupe_key=f"ride:{rid}", actor=device_id,
            refs=[x for x in (v.registration_id, v.wave_id, device_id,
                              v.window_id, bib) if x],
        )
        return {"status": "validated", "event": event.to_dict(), "verdict": v.to_dict(),
                "duplicate": False}

    # ============================================================ 离线验票/补传

    def device_manifest(self, device_id: str, at=None) -> dict:
        """生成断网期间可用的清单快照：有效号码布 + 权益窗口 + 策略。"""
        at = parse(at) if at else now()
        st = State.fold(self.log.all(), at)
        if device_id not in st.devices:
            raise NotFound(f"设备 {device_id} 未登记", "device_unknown")
        bibs = []
        for bib, rec in st.bibs.items():
            status = st.bib_status(bib, at)
            if status["decision"] == "allow":
                bibs.append({"bib": bib, "registration_id": status["registration_id"],
                             "wave_id": status["wave_id"]})
        windows = [
            {"window_id": w["window_id"], "mode": w["mode"], "start": w["start"],
             "end": w["end"], "benefit": w["benefit"], "waves": w.get("waves", [])}
            for w in st.windows.values()
        ]
        bundle_id = f"bundle-{device_id}-{format_dt(at).replace(':', '').replace('-', '')}"
        manifest = {
            "bundle_id": bundle_id, "device_id": device_id,
            "generated_at": format_dt(at), "tz": "Asia/Shanghai",
            "policy": st.policy, "valid_bibs": sorted(bibs, key=lambda x: x["bib"]),
            "windows": sorted(windows, key=lambda x: (x["mode"], x["start"])),
        }
        self.log.append("device_manifest_issued", {
            "device_id": device_id, "bundle_id": bundle_id,
            "generated_at": manifest["generated_at"],
            "bib_count": len(manifest["valid_bibs"]),
            "window_count": len(windows),
        }, occurred_at=at, actor=device_id, refs=(device_id,))
        return manifest

    @staticmethod
    def offline_record(ride_id: str, bib: str, mode: str, at: str,
                       local_decision: str, bundle_id: str) -> dict:
        """设备端在断网期间写出的一条待补传验票记录。"""
        return {"ride_id": ride_id, "bib": bib, "mode": mode, "at": format_dt(parse(at)),
                "local_decision": local_decision, "bundle_id": bundle_id, "online": False}

    def sync_device(self, device_id: str, records: list[dict], *,
                    recorded_at=None, actor="sync") -> dict:
        """网络恢复后补传：按各记录的原始发生时刻重新求值，逐条幂等入账。

        设备本地判定与平台按真实状态复算的结果不一致时，标记
        ``decision_match: false`` 供稽核，但不改变冻结写法本身。
        """
        sync_at = parse(recorded_at) if recorded_at else now()
        if device_id not in self._state().devices:
            raise NotFound(f"设备 {device_id} 未登记", "device_unknown")
        synced, duplicated, malformed = [], [], []
        for rec in records:
            try:
                rid = rec["ride_id"]
                bib = rec["bib"]
                mode = rec["mode"]
                occurred = parse(rec["at"])
            except (KeyError, TypeError, ValueError) as exc:
                malformed.append({"record": rec, "error": str(exc)})
                continue
            try:
                _, v = self.evaluate(device_id, bib, mode, occurred)
            except TransitError as exc:
                malformed.append({"record": rec, "error": f"{exc.code}: {exc}"})
                continue
            v.ride_id = rid
            match = (rec.get("local_decision") == v.decision)
            try:
                event = self.log.append(
                    "ride_validated",
                    {
                        "ride_id": rid, "device_id": device_id, "bib": bib,
                        "mode": mode, "at": format_dt(occurred), "online": False,
                        "decision": v.decision, "deny_reasons": v.reasons,
                        "window_id": v.window_id,
                        "registration_id": v.registration_id,
                        "wave_id": v.wave_id,
                        "bundle_id": rec.get("bundle_id"),
                        "local_decision": rec.get("local_decision"),
                        "decision_match": match,
                        "synced": True,
                        "basis": {**v.to_dict(), "device_id": device_id,
                                  "bib": bib, "frozen": True},
                    },
                    occurred_at=occurred, recorded_at=sync_at,
                    dedupe_key=f"ride:{rid}", actor=device_id,
                    refs=[x for x in (v.registration_id, v.wave_id, device_id,
                                      v.window_id, bib) if x],
                )
            except DuplicateReplay as dup:
                duplicated.append({"ride_id": rid,
                                   "first_recorded_at": dup.details["event"]["recorded_at"],
                                   "first_decision": dup.details["event"]["data"]["decision"]})
                continue
            synced.append({"ride_id": rid, "decision": v.decision,
                           "decision_match": match, "event_seq": event.seq})
        self.log.append("device_sync_completed", {
            "device_id": device_id, "synced": len(synced),
            "duplicated": len(duplicated), "malformed": len(malformed),
        }, occurred_at=sync_at, actor=actor, refs=(device_id,))
        return {"device_id": device_id, "recorded_at": format_dt(sync_at),
                "synced": synced, "duplicated": duplicated, "malformed": malformed}

    # ============================================================ 统计

    def ride_ledger(self, start=None, end=None) -> dict:
        """免费乘车核销台账（半开区间），按发生时刻统计。"""
        st = self._state()
        recorded = {e.data["ride_id"]: e.recorded_at
                    for e in self.log.by_type("ride_validated")}
        rows = []
        for ride in st.rides.values():
            at = ride["at"]
            if start and at < parse(start):
                continue
            if end and at >= parse(end):
                continue
            rows.append({
                "ride_id": ride["ride_id"], "at": format_dt(at),
                "recorded_at": recorded.get(ride["ride_id"]),
                "device_id": ride["device_id"], "bib": ride["bib"], "mode": ride["mode"],
                "decision": ride["decision"], "online": ride.get("online", True),
                "window_id": ride.get("window_id"), "wave_id": ride.get("wave_id"),
            })
        free = [r for r in rows if r["decision"] == "allow_free"]
        return {"total": len(rows), "free_rides": len(free),
                "denied": sum(1 for r in rows if r["decision"] == "deny"),
                "offline_synced": sum(1 for r in rows if not r["online"]),
                "rows": sorted(rows, key=lambda r: r["at"])}

    def _state(self) -> State:
        return State.fold(self.log.all())
