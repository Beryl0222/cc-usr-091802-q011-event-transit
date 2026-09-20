# 赛事交通权益联动

太原马拉松比赛日的**交通权益与运力调度服务**：把报名状态、号码布发放、四枪发令批次、
线路班次、车辆容量、临时道路管制、医院绿色通道纳入同一条**只增事件日志**，
对外提供可运行的验票、调度、改派、通知与赛后追溯能力。

全部为虚构演示数据，不含真实个人资料或业务凭据；所有时间以 **Asia/Shanghai（UTC+08:00）** 为准。

## 设计要点

| 需求 | 实现 |
| --- | --- |
| 一张凭证哪段时间有效 | 号码布状态 × 发令批次 × 权益窗口（地铁提前运营 / 分批次公交免费 / 赛后接驳），按**乘坐发生时刻**求值：`GET /bibs/{bib}/entitlements?at=...` |
| 断网可验票、恢复后补传 | 设备先取清单快照（`GET /devices/{id}/manifest`），离线写本地记录，恢复后 `POST /devices/{id}/sync` 按各记录原始发生时刻复算入账 |
| 重放不得重复占用容量 | 每条业务记录有唯一去重键（`ride:<ride_id>`、`board:<trip>:<ride>`、`medres:<trip>:<case>`、`receipt:<note>:<channel>`）；命中即返回 `duplicate`，容量不变 |
| 挂失/退赛/改枪只改生效后的行程 | 这些事件只改变其发生时刻之后的折叠结果；每次核销把当时的判定依据写入 `basis`（含批次/窗口）并冻结，历史通行永不回改 |
| 普通接驳不占医疗转运资源 | 车辆/线路分 `shuttle` 与 `medical` 两类，双向拒绝；医疗预留座位对普通接驳不可见；医疗上车还要求医院绿色通道开启、路径未被封控 |
| 道路突然封闭只调未发车班次 | 以封控时刻为准，仅调整 `scheduled` 班次：沿站点图 BFS 重算绕行（走平行支桥），无绕行才取消；已发车班次不动 |
| 替代路线与通知回执 | 每个受影响站点收到带 `alternative_segments` 的通知，送达以回执为准；回执可断网补传、按渠道去重 |
| 拥堵告警查清来龙去脉 | `GET /alerts/{id}/trace` 沿「批次 → 选手核销 → 班次/车辆 → 封控路段 → 处置决定 → 站点通知/回执」组装因果链与有序时间线 |
| 按原始时区重放 | `GET /replay?at=...[&axis=occurred|recorded]`：occurred 轴回答"那一刻真实的运力/核销"（含补传），recorded 轴回答"调度员当时屏幕所见"；未送达通知同步给出 |

### 两条时间轴

每条事件同时保存 `occurred_at`（业务发生的本地墙钟时间）与 `recorded_at`（平台登记时间）。
离线验票 09:45 发生、10:00 补传：在 occurred 轴 09:50 已可见，在 recorded 轴要到 10:00 才出现。
状态折叠、容量、通知送达均可选轴重放。

## 运行

```bash
python3 service.py --check                      # 基础自检
python3 service.py --port 8000 --demo           # 预置太原四枪演示场景
python3 service.py --port 8000 --store ev.jsonl # 事件日志 JSONL 持久化（重启不丢、去重仍生效）

python3 -m unittest discover -s tests -v        # 45 项契约测试
python3 -m event_transit.demo                   # 命令行跑完演示故事线并打印溯源时间线
```

## HTTP 接口（均为 JSON）

* 基础数据：`POST /waves` `/windows` `/devices` `/stops` `/segments` `/routes` `/vehicles` `/hospitals`，`POST /hospitals/green-channel`
* 选手：`POST /runners` `/bibs/issue` `/bibs/lost` `/bibs/replace` `/runners/withdraw` `/runners/change-wave`
* 验票：`POST /rides/validate`，`GET /rides/ledger`，`GET /devices/{id}/manifest`，`POST /devices/{id}/sync`
* 班次运力：`POST /trips`，`GET /trips/{id}`，`POST /trips/{id}/depart` `/complete` `/board`，`POST /medical/reserve` `/medical/transport`，`GET /fleet`
* 管制通知告警：`POST /roads/close` `/roads/reopen`，`POST /notifications/{id}/receipt`，`POST /alerts`，`POST /alerts/{id}/handle`
* 追溯：`GET /alerts/{id}/trace`，`GET /replay`，`GET /events`；`POST /demo/load` 一键载入演示数据

成功重放同一业务记录返回 `200 {"status":"duplicate"}`（安全可重试）；业务冲突 409，
规则拒绝 422（如 `medical_resource_protected`、`capacity_exceeded`、`green_channel_closed`），未知资源 404。

## 代码结构

```
event_transit/
  clock.py          # Asia/Shanghai 时区与双时间轴解析
  events.py         # 只增事件日志：去重键、JSONL 持久化
  model.py          # 事件 → 任意时刻状态折叠（效力/容量/封控/通知）
  entitlements.py   # 报名、号码布、批次窗口、验票、离线清单与补传
  dispatch.py       # 路网班次、容量、医疗隔离、封控改派、通知回执、拥堵告警
  trace.py          # 告警因果链溯源、双轴时刻重放
  demo.py           # 太原四枪完整故事线（虚构数据）
  api.py            # HTTP 接口
tests/              # 事件日志 / 权益 / 调度 / 溯源 / HTTP 五组契约
fixtures/sample.json
```

## 演示故事线（`--demo`）

四枪批次与三类权益窗口 → 地铁/公交验票（窗外拒绝、批次不匹配拒绝）→ 车载设备断网放行
→ 改枪、号码布挂失（旧通行依据保留）→ 接驳容量与超售拒绝、医疗预留与转运（双向隔离、绿色通道）
→ 长风桥 09:50 封控：09:40 已发车次不动、10:10 未发车次改走支桥 B-X-Y-C、无绕行单线取消
→ B 站回执送达 / C 站回执缺失 → 拥堵告警与处置决定 → 补传复算（挂失后离线放行被 deny 并标记 `decision_match=false`）
→ 重放整批记录 0 新增 1 重复 → 赛后溯源与多点重放。
