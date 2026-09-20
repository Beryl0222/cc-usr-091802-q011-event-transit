"""权益域：报名、号码布、发令批次、凭证有效窗口、免费乘车核销。

时间语义（对应调度规则）：
- 所有状态都由“截至该时刻”的事件重放得到，因此挂失 / 退赛 / 改枪
  只影响生效时刻之后的行程；更早完成的通行在重放时仍看到旧状态，
  原有依据自然保留，不做追溯翻案。
- 离线闸机/车载设备依据下载的权益快照验票，通行先落本地，联网后补传；
  补传事件携带设备生成的幂等编号，重放绝不重复占用容量。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .clock import parse_event_time
from .events import Event, EventStore

# ---- 事件类型 -------------------------------------------------------------

RUNNER_REGISTERED = "RunnerRegistered"
WAVE_CONFIGURED = "WaveConfigured"
WAVE_CHANGED = "WaveChanged"            # 改枪
BIB_ISSUED = "BibIssued"
BIB_REPORTED_LOST = "BibReportedLost"   # 挂失
BIB_REPLACEMENT_ISSUED = "BibReplacementIssued"
RUNNER_WITHDRAWN = "RunnerWithdrawn"    # 退赛
BENEFIT_WINDOW_OPENED = "BenefitWindowOpened"
SNAPSHOT_ISSUED = "EntitlementSnapshotIssued"  # 断网前下发设备
RIDE_REDEEMED = "RideRedeemed"          # 一次核销（接受或拒绝都留痕）


@dataclass
class RideResult:
    accepted: bool
    reason: str | None
    basis: str           # online / offline-snapshot
    event_id: str
    warnings: list


class EntitlementDomain:
    def __init__(self, store: EventStore):
        self.store = store

    # ---- 登记类命令 -----------------------------------------------------

    def configure_wave(self, wave_id: str, label: str, start_at,
                       configured_at=None, wave_idem: str | None = None):
        start = parse_event_time(start_at)
        at = parse_event_time(configured_at) if configured_at else start
        event, _ = self.store.append(
            WAVE_CONFIGURED,
            {"wave_id": wave_id, "label": label, "start_at": start.isoformat()},
            at,
            event_id=wave_idem,
        )
        return event

    def register_runner(self, runner_id: str, wave_id: str, at, event_id: str | None = None):
        at = parse_event_time(at)
        event, _ = self.store.append(
            RUNNER_REGISTERED,
            {"runner_id": runner_id, "wave_id": wave_id},
            at,
            event_id=event_id,
        )
        return event

    def change_wave(self, runner_id: str, to_wave: str, at, event_id: str | None = None):
        """改枪：只改变生效时刻之后的行程。"""
        at = parse_event_time(at)
        state = build_entitlement_state(self.store, as_of=at)
        runner = state["runners"].get(runner_id)
        if runner is None:
            raise LookupError(f"选手不存在: {runner_id}")
        if to_wave not in state["waves"]:
            raise LookupError(f"发令批次不存在: {to_wave}")
        event, _ = self.store.append(
            WAVE_CHANGED,
            {"runner_id": runner_id, "from_wave": runner["wave_id"], "to_wave": to_wave},
            at,
            event_id=event_id,
        )
        return event

    def issue_bib(self, runner_id: str, bib: str, at, event_id: str | None = None):
        at = parse_event_time(at)
        event, _ = self.store.append(
            BIB_ISSUED, {"runner_id": runner_id, "bib": bib}, at, event_id=event_id
        )
        return event

    def report_lost(self, bib: str, at, event_id: str | None = None):
        """挂失：原号码布自生效时刻起失效（不改变此前通行）。"""
        at = parse_event_time(at)
        event, _ = self.store.append(
            BIB_REPORTED_LOST, {"bib": bib}, at, event_id=event_id
        )
        return event

    def issue_replacement(self, runner_id: str, new_bib: str, at, event_id: str | None = None):
        """补领新号码布；权益随选手转移。"""
        at = parse_event_time(at)
        event, _ = self.store.append(
            BIB_REPLACEMENT_ISSUED,
            {"runner_id": runner_id, "bib": new_bib},
            at,
            event_id=event_id,
        )
        return event

    def withdraw(self, runner_id: str, at, event_id: str | None = None):
        """退赛：只改变生效后的行程，已完成的通行保留。"""
        at = parse_event_time(at)
        event, _ = self.store.append(
            RUNNER_WITHDRAWN, {"runner_id": runner_id}, at, event_id=event_id
        )
        return event

    def open_window(self, window: dict, at=None, event_id: str | None = None):
        """登记凭证有效窗口，如地铁提前运营、凭号码布免费乘公交。

        window: {window_id, mode(metro|bus|shuttle), title, start_at,
                 end_at, waves(None=全部批次 或 [wave_id...]), note}
        """
        start = parse_event_time(window["start_at"])
        end = parse_event_time(window["end_at"])
        if end <= start:
            raise ValueError("窗口结束时间必须晚于开始时间")
        at = parse_event_time(at) if at else start
        payload = {
            "window_id": window["window_id"],
            "mode": window["mode"],
            "title": window.get("title", window["window_id"]),
            "start_at": start.isoformat(),
            "end_at": end.isoformat(),
            "waves": window.get("waves"),
            "note": window.get("note", ""),
        }
        event, _ = self.store.append(BENEFIT_WINDOW_OPENED, payload, at, event_id=event_id)
        return event

    # ---- 离线支持 -------------------------------------------------------

    def issue_snapshot(self, device_id: str, at, event_id: str | None = None) -> dict:
        """给断网设备下发一份当前权益快照（含全部有效窗口与有效号码布）。"""
        at = parse_event_time(at)
        state = build_entitlement_state(self.store, as_of=at)
        active = []
        for bib, info in sorted(state["bibs"].items()):
            runner = state["runners"].get(info["runner_id"])
            if not info["active"] or not runner or runner["status"] != "registered":
                continue
            active.append({"bib": bib, "runner_id": info["runner_id"], "wave_id": runner["wave_id"]})
        snapshot = {
            "device_id": device_id,
            "issued_at": at.isoformat(),
            "windows": list(state["windows"].values()),
            "active_bibs": active,
        }
        self.store.append(
            SNAPSHOT_ISSUED,
            {"snapshot_id": event_id or "", "device_id": device_id, **{
                k: snapshot[k] for k in ("issued_at", "windows", "active_bibs")
            }},
            at,
            event_id=event_id,
            device_id=device_id,
        )
        return snapshot

    def upload_offline_scans(self, device_id: str, scans: list[dict]) -> list[RideResult]:
        """联网后补传一批断网期间的本地通行记录。

        每条 scan: {redeem_id, bib, window_id, trip_id?, scanned_at,
                    device_result(可选，设备本地判定)}
        幂等：redeem_id 重复直接返回既有结果，不重复计数。
        设备已经放行的通行不因补传时状态变化而推翻，但会给出比对告警。
        """
        results = []
        for scan in scans:
            results.append(
                self.redeem(
                    bib=scan["bib"],
                    window_id=scan["window_id"],
                    at=scan["scanned_at"],
                    trip_id=scan.get("trip_id"),
                    device_id=device_id,
                    redeem_id=scan["redeem_id"],
                    basis="offline-snapshot",
                    snapshot_id=scan.get("snapshot_id"),
                    device_result=scan.get("device_result", "accepted"),
                )
            )
        return results

    # ---- 核销 -----------------------------------------------------------

    def redeem(
        self,
        bib: str,
        window_id: str,
        at,
        trip_id: str | None = None,
        device_id: str | None = None,
        redeem_id: str | None = None,
        basis: str = "online",
        snapshot_id: str | None = None,
        device_result: str = "accepted",
    ) -> RideResult:
        """核销一次免费乘车/进站。

        在线：按“核销发生时刻”重放状态判定，无效则拒绝且不占容量。
        离线补传：沿用设备当时判定（已完成的通行保留依据），
        服务端只做时后比对并把差异写进 warnings。
        """
        at = parse_event_time(at)
        existing = self.store.get(redeem_id) if redeem_id else None
        if existing is not None:
            p = existing.payload
            return RideResult(
                accepted=p["accepted"], reason=p.get("reason"),
                basis=p.get("basis", "online"), event_id=existing.event_id,
                warnings=["duplicate_replay_ignored"],
            )

        decision = self._evaluate(bib, window_id, at, trip_id)
        warnings: list = []
        if basis == "offline-snapshot":
            # 设备已放行的记录不翻案；仅当按其“通行当时”的状态也不成立时标注差异
            accepted = device_result == "accepted"
            reason = None if accepted else (device_result or "device_denied")
            if accepted and decision[0] is False:
                warnings.append(f"offline_basis_mismatch:{decision[1]}")
        else:
            accepted, reason, _mode = decision

        payload = {
            "bib": bib,
            "window_id": window_id,
            "mode": decision[2],
            "trip_id": trip_id,
            "accepted": accepted,
            "reason": reason,
            "basis": basis,
            "snapshot_id": snapshot_id,
            "warnings": warnings,
        }
        event, _ = self.store.append(
            RIDE_REDEEMED, payload, at, event_id=redeem_id, device_id=device_id
        )
        return RideResult(accepted, reason, basis, event.event_id, warnings)

    def _evaluate(self, bib: str, window_id: str, at: datetime, trip_id: str | None):
        """返回 (accepted, reason|None, mode)。容量原因交由调度域判定。"""
        state = build_entitlement_state(self.store, as_of=at)
        window = state["windows"].get(window_id)
        if window is None:
            return False, "unknown_window", None
        mode = window["mode"]
        if at < parse_event_time(window["start_at"]) or at > parse_event_time(window["end_at"]):
            return False, "outside_window", mode
        bib_info = state["bibs"].get(bib)
        if bib_info is None:
            return False, "unknown_bib", mode
        if not bib_info["active"]:
            return False, "bib_lost", mode
        runner = state["runners"].get(bib_info["runner_id"])
        if runner is None:
            return False, "not_registered", mode
        if runner["status"] == "withdrawn":
            return False, "withdrawn", mode
        waves = window.get("waves")
        if waves and runner["wave_id"] not in waves:
            return False, "wave_not_eligible", mode
        return True, None, mode


# ---- 投影 ------------------------------------------------------------------

def build_entitlement_state(store: EventStore, as_of: datetime | str | None = None) -> dict:
    """把截至 as_of 的权益事件折叠成当时的状态（供判定与重放）。"""
    waves: dict = {}
    runners: dict = {}
    bibs: dict = {}
    windows: dict = {}
    for event in store.replay(as_of):
        p = event.payload
        if event.event_type == WAVE_CONFIGURED:
            waves[p["wave_id"]] = {
                "wave_id": p["wave_id"], "label": p["label"], "start_at": p["start_at"],
            }
        elif event.event_type == RUNNER_REGISTERED:
            runners[p["runner_id"]] = {
                "runner_id": p["runner_id"], "wave_id": p["wave_id"],
                "status": "registered",
            }
        elif event.event_type == WAVE_CHANGED:
            if p["runner_id"] in runners:
                runners[p["runner_id"]]["wave_id"] = p["to_wave"]
        elif event.event_type == BIB_ISSUED:
            bibs[p["bib"]] = {"bib": p["bib"], "runner_id": p["runner_id"], "active": True}
        elif event.event_type == BIB_REPLACEMENT_ISSUED:
            bibs[p["bib"]] = {"bib": p["bib"], "runner_id": p["runner_id"], "active": True}
        elif event.event_type == BIB_REPORTED_LOST:
            if p["bib"] in bibs:
                bibs[p["bib"]]["active"] = False
        elif event.event_type == RUNNER_WITHDRAWN:
            if p["runner_id"] in runners:
                runners[p["runner_id"]]["status"] = "withdrawn"
        elif event.event_type == BENEFIT_WINDOW_OPENED:
            windows[p["window_id"]] = dict(p)
    return {"waves": waves, "runners": runners, "bibs": bibs, "windows": windows}


def list_redemptions(store: EventStore, as_of=None) -> list[dict]:
    """重放各时刻的免费乘车核销。"""
    rows = []
    for event in store.replay(as_of, types=[RIDE_REDEEMED]):
        p = event.payload
        runner = None
        state = build_entitlement_state(store, as_of=event.event_time)
        info = state["bibs"].get(p["bib"])
        if info:
            runner = info["runner_id"]
        rows.append({
            "event_id": event.event_id,
            "at": event.event_time.isoformat(),
            "recorded_at": event.recorded_at.isoformat(),
            "bib": p["bib"],
            "runner_id": runner,
            "wave_id": (state["runners"].get(runner, {}) or {}).get("wave_id") if runner else None,
            "window_id": p["window_id"],
            "mode": p.get("mode"),
            "trip_id": p.get("trip_id"),
            "accepted": p["accepted"],
            "reason": p.get("reason"),
            "basis": p.get("basis", "online"),
            "warnings": p.get("warnings", []),
            "device_id": event.device_id,
        })
    return rows
