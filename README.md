# 赛事交通权益联动

太原马拉松比赛日的**交通权益与调度服务**：把报名状态、号码布发放、四枪发令批次、
线路班次、车辆容量、临时管制与医院绿色通道纳入同一套可运行系统，并以
**事件溯源（event sourcing）**回答“一张凭证在哪段时间有效、哪辆车还能接人、
一条告警的来龙去脉是什么”。

## 它如何对应调度规则

| 业务要求 | 实现方式 |
| --- | --- |
| 一张凭证在哪段时间有效 | 权益窗口 `BenefitWindow`（地铁提前运营 / 凭号码布免费公交 / 赛后接驳），核销时按**通行当时**的窗口、号码布、报名、批次状态判定 |
| 四枪发令同时生效 | A/B/C/D 四个批次，窗口可限定适用批次；所有核销/登乘记录当时所属批次 |
| 闸机/车载设备断网可验票、联网补传 | `issue_snapshot` 下发含有效窗口与号码布的快照；设备本地放行，`upload_offline_scans` 补传，按事件发生时刻重放 |
| 同一记录重放不重复占用容量 | 事件以 `event_id`（核销为 `redeem_id`）幂等去重；座位占用是幂等集合，重放重传只算一次 |
| 挂失 / 退赛 / 改枪只改变生效后的行程 | 这些都是新事件；任意时刻的状态由“截至该时刻”的事件重放得到，更早完成的通行在历史投影里仍看到旧状态，不追溯翻案。离线设备已放行的通行保留，仅产生 `offline_basis_mismatch` 差异告警 |
| 普通接驳不得占用医疗转运资源 | 普通车 / 救护车分池：普通乘客登乘医疗车返回 `medical_resource_reserved`；医疗请求只从救护车池分配，并打开医院绿色通道 |
| 道路突然封闭只调整未发车班次 | `impose_closure` 只处置 `scheduled/diverted` 的班次：有替代线路则改道，否则取消；`departed/completed` 原样保留；救护车不被普通改派波及 |
| 替代路线 + 通知回执 | 每条管制派生一条站点通知（含替代路线、改道/取消明细）；`record_receipt` 记录逐站回执，`/api/replay/undelivered` 列出仍缺回执的站点 |
| 拥堵告警查清来龙去脉 | `/api/trace/<alert_id>` 沿 **发令批次 → 选手 → 车辆/班次 → 路段 → 处置决定 → 通知回执 → 医疗转运** 聚合成按原始时刻排序的时间线 |
| 按原始时区重放 | 所有时间带时区（`Asia/Shanghai`, UTC+8），事件存双时间 `event_time`（业务发生）/ `recorded_at`（入库）；所有查询支持 `?as_of=` |

## 架构

```
transit/
  clock.py          赛事时区、时间解析
  events.py         追加式事件日志（幂等、双时间、JSONL、按时刻重放）
  entitlements.py   报名/号码布/批次/窗口/核销/离线快照投影
  dispatch.py       线路/车辆/班次/容量/管制改派/通知/医疗投影
  trace.py          告警链路追溯
  service.py        门面聚合 + build_demo() 四枪发令演示场景
service.py          HTTP API 入口（保持原 /health 契约）
tests/              37 个用例（unittest/pytest 均可）
```

事实只追加、不修改；状态是事件的投影。因此“回到任意时刻重放运力、核销、
未送达通知”只是换一个 `as_of` 截止时间重新折叠事件。

## 运行

```bash
python3 service.py --check             # 契约与关键规则自检
python3 service.py --demo              # 打印演示场景的重放与追溯摘要
python3 service.py --demo --serve --port 8000 --store race.jsonl
python3 -m unittest discover -s tests  # 或 python3 -m pytest -q
```

## HTTP API

只读重放（`as_of` 为可选 ISO 时间，无时区按 Asia/Shanghai 解释）：

- `GET /health`
- `GET /api/replay/capacity?as_of=...`      各班次运力（普通/医疗分列，改道后显示实际线路）
- `GET /api/replay/redemptions?as_of=...`  免费乘车核销（含批次、在线/离线依据、差异告警）
- `GET /api/replay/undelivered?as_of=...`  缺站点回执的通知
- `GET /api/replay/entitlements` / `/api/replay/dispatch`
- `GET /api/alerts`
- `GET /api/trace/<alert_id>?as_of=...`    告警全链路
- `GET /api/events`                        原始事件日志

命令（POST JSON，均可带 `event_id` 实现客户端幂等）：

- `POST /api/command/{register_runner|change_wave|issue_bib|report_lost|issue_replacement|withdraw|open_window|redeem|issue_snapshot|upload_offline_scans}`
- `POST /api/command/{register_route|register_alternative|register_vehicle|register_hospital|schedule_trip|mark_departed|mark_completed|board|impose_closure|lift_closure|record_receipt|raise_alert|request_medical|deliver_patient}`

示例：

```bash
curl -X POST localhost:8000/api/command/impose_closure -H 'Content-Type: application/json' -d '{
  "restriction_id":"R-918","segment_id":"BUS81:1",
  "start_at":"2026-09-20T07:05:00+08:00","end_at":"2026-09-20T10:00:00+08:00",
  "reason":"救援通道临时占用"}'
```

## 演示场景覆盖

`build_demo()` 以 2026-09-20 四枪发令为背景，包含：地铁/公交/赛后三个权益窗口；
T1 满员告警（3 人登乘、第 4 人被拒）；R003 改枪（C→B）前后两笔地铁核销批次不同；
B0004 挂失前公交通行保留、挂失后闸机离线放行带差异告警、补领后 R004 退赛再核销被拒；
滨河路口拥堵告警后道路封闭（已发 T1/T2 不动、未发 T3 改道 BUS99、四站回执齐全）；
赛后段无预警封闭致 T4 取消、太原南站缺回执；医疗点伤员经救护车 MED-1 转运并开通绿色通道。

`fixtures/sample.json` 仅保存可公开的领域样例（时间边界与规则），不含真实个人资料或业务凭据。
