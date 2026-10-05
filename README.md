# 家庭储能时段调度 API

基于 **Python + FastAPI + Pydantic** 的家庭储能调度服务。输入等长的半小时电价、基础负荷、光伏预测序列以及电池参数，用**动态规划（SOC 离散到 0.1 kWh）**求出满足功率、SOC 和末端 SOC 约束的**最低总成本**充放电计划；另提供 `/telemetry/audit` 对半小时实测记录做能量守恒与购电量**审计**（只核对标记，不修正读数、不生成调度计划）；`/dispatch/settlement` 对一份已下发计划做**执行结算**：携带原始 `/dispatch` 输入与计划快照，服务重算校验快照未被篡改、复用遥测审计逐条核对，并只对首个异常 / SOC 断点之前的连续有效区间按实测购电量与吞吐量计费；`/dispatch/replan` 支持计划**执行到中途**时依据连续实测前缀**续排**：核对原计划与前缀审计、保留实测原记录并从实测末态重排余段，同时返回可直接交给结算接口的余段调度输入与计划快照。`/dispatch/sessions` 进一步提供**可持久接续的执行会话**：实测记录可分多批到达，创建会话时提交原 `/dispatch` 输入、完整计划快照与审计容差（先重算确认快照一致），之后按时段下标成批追加记录，首批首态接原初始 SOC、后续批次首态接已保存末态，沿用现有遥测审计逐段核对且**整批原子入库**；批次边界与首次回执随记录同事务落库，**起点、长度、内容都与某已确认整批一致**的重试幂等返回该批首收回执（不随后续批次漂移），短批、内容变更或跨批拼接等重叠上报一律 409、只有一份生效；会话与记录落在 **SQLite（命名卷）**，进程重启后仍可查询、续报，未完成前缀可直接续排、满期记录可直接结算。满期（`complete`）会话还支持**计量表更正**：按会话经 HTTP 提交期望修订号与若干「时段下标 + 完整替换记录」，服务端在当前版本上叠加（未指定时段保持原值）并用会话容量、效率、初始 SOC 与审计容差**重新核对整段台账**，下标重复/越界、任一时段能量失衡或相邻 SOC 断点都整次拒绝、已有修订不变；核对通过后原子落盘修订号与修订台账（修订号从 0 起单调递增，过期返回 409 与当前号）。**最初上报记录、原会话查询、批次重试判定与首收回执永不被更正改写**；更正查询返回原始与当前修订记录、差异下标、历次更正，以及可直接提交现有结算接口的完整请求数据；重启后修订仍可查询并继续更正。满期会话还可经 `POST /dispatch/sessions/{id}/vouchers` 按修订号**出具逐版结算凭证**：修订号 0 对最初上报记录、其余版本对对应更正台账，沿用现有结算规则在同一写事务内核算落盘（**只能结算有效前缀时整次拒绝且不留下凭证**）；凭证保存修订号、结算摘要、实测总费用、出单时间并冻结该版台账与完整结算报告，**首张记全额、以后每张列出相对上一张的应补或应退差额（允许跳过未出单的中间修订）**，同一修订号重试始终返回原凭证（即使后来发生更正），未出单的过期修订号返回 409 及当前号；`GET .../vouchers` 按出单次序查询凭证并返回当前修订待出单状态，凭证持久化在命名卷 SQLite 中，重启后仍可查，出单与并发更正互斥使凭证不会引用过期台账。

## 一条命令启动

需要 Docker 20.10+（带 Compose v2）：

```bash
docker compose up --build
```

服务启动后：

- 健康检查：<http://localhost:8000/healthz>
- 交互式文档（Swagger UI）：<http://localhost:8000/docs>
- OpenAPI Schema：<http://localhost:8000/openapi.json>

停止：`Ctrl+C`（后台运行用 `docker compose up -d --build`，停止用 `docker compose down`）。

本地裸机运行（Python 3.11+）：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 接口

### `GET /healthz`

```json
{"status": "ok"}
```

### `POST /dispatch`

请求体（每个时段固定 **0.5 小时**，三个数组必须等长，长度 1–200）：

| 字段 | 单位 | 说明 |
|---|---|---|
| `prices` | 元/kWh | 各时段购电电价（非负） |
| `base_load_kw` | kW | 各时段基础负荷 |
| `pv_kw` | kW | 各时段光伏预测功率 |
| `capacity_kwh` | kWh | 电池容量，必须为 0.1 的整数倍 |
| `max_charge_kw` / `max_discharge_kw` | kW | 最大充 / 放电**交流侧**功率 |
| `charge_efficiency` / `discharge_efficiency` | – | 充 / 放电效率，取值 (0, 1] |
| `initial_soc_kwh` | kWh | 初始 SOC（0.1 整数倍，≤ 容量） |
| `min_terminal_soc_kwh` | kWh | 最低末端 SOC（0.1 整数倍，≤ 容量） |
| `degradation_cost_per_kwh` | 元/kWh | 按电芯吞吐量计的循环衰减成本 |

示例：

```bash
curl -s -X POST http://localhost:8000/dispatch \
  -H 'Content-Type: application/json' \
  -d '{
    "prices": [1.0, 0.0, 1.0],
    "base_load_kw": [0.2, 0.2, 0.2],
    "pv_kw": [0.0, 0.0, 0.0],
    "capacity_kwh": 0.2,
    "max_charge_kw": 1.0,
    "max_discharge_kw": 1.0,
    "charge_efficiency": 1.0,
    "discharge_efficiency": 1.0,
    "initial_soc_kwh": 0.0,
    "min_terminal_soc_kwh": 0.0,
    "degradation_cost_per_kwh": 0.0
  }'
```

响应：`periods` 为逐时段计划，`summary` 为汇总。

```json
{
  "periods": [
    {
      "index": 0,
      "action": "idle",                 // charge / discharge / idle
      "action_energy_kwh": 0.0,         // 电芯侧能量变化：正=充，负=放，0.1 整数倍
      "charge_ac_energy_kwh": 0.0,      // 该时段电池吸收的交流电量
      "discharge_ac_energy_kwh": 0.0,   // 该时段电池放出的交流电量
      "battery_power_kw": 0.0,          // 正=充电功率，负=放电功率
      "grid_purchase_energy_kwh": 0.1,  // 购电量
      "pv_curtailed_energy_kwh": 0.0,   // 弃光量
      "soc_start_kwh": 0.0,
      "soc_end_kwh": 0.0,
      "energy_cost_yuan": 0.1,          // 该时段能量费
      "degradation_cost_yuan": 0.0      // 该时段衰减费
    }
  ],
  "summary": {
    "num_periods": 3,
    "period_hours": 0.5,
    "soc_resolution_kwh": 0.1,
    "total_energy_cost_yuan": 0.1,
    "total_degradation_cost_yuan": 0.0,
    "total_cost_yuan": 0.1,
    "total_grid_purchase_kwh": 0.2,
    "peak_grid_purchase_energy_kwh": 0.2,
    "peak_grid_purchase_power_kw": 0.4,
    "battery_throughput_kwh": 0.1,
    "terminal_soc_kwh": 0.0
  }
}
```

### `POST /telemetry/audit`

对按时间顺序排列的半小时**实测记录**逐条做台账核对：电芯侧能量守恒、购电量功率平衡、相邻记录 SOC 衔接。只审计并标记异常，**不修正读数、不生成调度计划**。

请求体：

| 字段 | 单位 | 说明 |
|---|---|---|
| `capacity_kwh` | kWh | 电池容量，须为正 |
| `charge_efficiency` / `discharge_efficiency` | – | 充 / 放电效率，取值 (0, 1] |
| `energy_tolerance_kwh` | kWh | 能量容差（非负）；任一分项残差绝对值**严格大于**它即判该条异常 |
| `records` | – | 按序排列的半小时实测记录，长度 1–2000 |

`records` 每条记录的字段（均须有限且非负，SOC 不得超过容量）：

| 字段 | 单位 | 说明 |
|---|---|---|
| `base_load_kw` | kW | 基础负荷功率 |
| `pv_kw` | kW | 光伏功率 |
| `charge_ac_energy_kwh` / `discharge_ac_energy_kwh` | kWh | 交流侧充 / 放电量 |
| `grid_purchase_energy_kwh` | kWh | 电网购电量 |
| `soc_start_kwh` / `soc_end_kwh` | kWh | 时段首 / 末 SOC |

逐条核算规则（与 `/dispatch` 的模型假设一致）：

```
期望末态 SOC = 首态 SOC + 交流充电量×充电效率 − 交流放电量÷放电效率
期望购电量   = max(0, (基础负荷 − 光伏)×0.5 + 交流充电量 − 交流放电量)
SOC 衔接差   = 首态 SOC − 上一条记录的末态 SOC（首条记 0）
```

响应中每条记录给出三个分项残差（实测 − 核算）：`soc_residual_kwh`、`grid_residual_kwh`、`soc_continuity_gap_kwh`，任一绝对值超容差则该条 `anomalous=true` 并在 `reasons` 列出原因；顶层 `anomalous_records` 汇总异常下标。审计结果正常返回 **HTTP 200**（异常是核对结论，不是请求错误）。

示例（第二条记录购电量实测 0.7，核算仅 0.55，残差 0.15 超容差）：

```bash
curl -s -X POST http://localhost:8000/telemetry/audit \
  -H 'Content-Type: application/json' \
  -d '{
    "capacity_kwh": 10.0,
    "charge_efficiency": 0.9,
    "discharge_efficiency": 0.9,
    "energy_tolerance_kwh": 0.05,
    "records": [
      {"base_load_kw": 1.0, "pv_kw": 0.4, "charge_ac_energy_kwh": 0.5,
       "discharge_ac_energy_kwh": 0.0, "grid_purchase_energy_kwh": 0.8,
       "soc_start_kwh": 2.0, "soc_end_kwh": 2.45},
      {"base_load_kw": 2.0, "pv_kw": 0.0, "charge_ac_energy_kwh": 0.0,
       "discharge_ac_energy_kwh": 0.45, "grid_purchase_energy_kwh": 0.7,
       "soc_start_kwh": 2.45, "soc_end_kwh": 1.95}
    ]
  }'
```

```json
{
  "num_records": 2,
  "period_hours": 0.5,
  "energy_tolerance_kwh": 0.05,
  "anomalous": true,
  "anomalous_records": [1],
  "records": [
    {
      "index": 0,
      "expected_soc_end_kwh": 2.45,
      "soc_residual_kwh": 0.0,
      "expected_grid_purchase_energy_kwh": 0.8,
      "grid_residual_kwh": 0.0,
      "soc_continuity_gap_kwh": 0.0,
      "anomalous": false,
      "reasons": []
    },
    {
      "index": 1,
      "expected_soc_end_kwh": 1.95,
      "soc_residual_kwh": 0.0,
      "expected_grid_purchase_energy_kwh": 0.55,
      "grid_residual_kwh": 0.15,
      "soc_continuity_gap_kwh": 0.0,
      "anomalous": true,
      "reasons": ["购电量残差 +0.150000kWh 超容差 0.05kWh（实测 0.7kWh，核算值 0.55kWh）"]
    }
  ]
}
```

### `POST /dispatch/settlement`

对已下发的调度计划做执行结算。请求携带四样东西：

| 字段 | 说明 |
|---|---|
| `dispatch` | 一次 `/dispatch` 的**完整输入**（电价/负荷/光伏序列 + 全部电池参数） |
| `plan_snapshot` | 当初 `/dispatch` 返回的计划快照（`periods` + `summary` 原样回传） |
| `settlement_tolerance_kwh` | 结算容差（kWh，非负）；遥测三类残差与 SOC 轨迹偏离均按「绝对值严格大于容差」判定 |
| `records` | 与计划**等长**的半小时实测记录（字段同 `/telemetry/audit`） |

服务端处理顺序：

1. **重算防篡改**：用现有求解器对 `dispatch` 重算，逐字段（动作、各能量、SOC、逐时段费用、summary）比对 `plan_snapshot`，任一处不一致整体拒绝（`plan_snapshot_mismatch`）；
2. **遥测审计**：复用 `/telemetry/audit` 的同一套核算，逐条检查电芯能量守恒、购电量平衡、相邻记录 SOC 衔接；
3. **SOC 断点**：额外比对每条记录的首 / 末 SOC 与计划 SOC 轨迹，偏差超容差即为断点（实际执行偏离了计划状态轨迹，后续时段的计划口径不再可比）；
4. **冻结待结算**：从**首条异常或 SOC 断点**（含该条）起，后续记录一律标为 `pending`（待结算），即使后面数据恢复自洽也不继续计费；
5. **有效区间计费**：对此前连续有效前缀（下标 0 起），逐时段按**实测购电量**算能量费、按**实测电芯吞吐量**（交流充电×ηc + 交流放电÷ηd）算衰减费，并给出与计划费用的差额及逐时段偏差。

`status=settled` 表示全部记录连续有效、整体结算；`status=partially_settled` 表示存在异常 / 断点，仅前缀结算。实测与预测的偏差（例如实际负荷高于预测导致多购电，但 SOC 轨迹不变）不属于异常，照常结算并计入费用差额。

整体拒绝（HTTP 422）的情形：

- `plan_snapshot_mismatch`：快照与重算结果不一致（被篡改或缺字段）；重算本身无解时为 `infeasible`；
- `invalid_input`：记录长度与计划时段数不符、容差为负 / 非有限、测量值为负 / 非有限、SOC 超容量等；
- `no_valid_interval`：**首条**记录即异常或 SOC 断点，不存在任何有效区间。

示例（全部记录有效；t1 购电表读数 0.015 比计划多 0.005kWh，在 0.01kWh 容差内，照常结算并计 0.005 元能量费差额）：

```bash
curl -s -X POST http://localhost:8000/dispatch/settlement \
  -H 'Content-Type: application/json' \
  -d '{
    "dispatch": {
      "prices": [0.0, 1.0, 1.0, 0.0],
      "base_load_kw": [0.2, 0.2, 0.2, 0.2],
      "pv_kw": [0.0, 0.0, 0.0, 0.0],
      "capacity_kwh": 0.4,
      "max_charge_kw": 1.0,
      "max_discharge_kw": 1.0,
      "charge_efficiency": 0.9,
      "discharge_efficiency": 0.9,
      "initial_soc_kwh": 0.0,
      "min_terminal_soc_kwh": 0.0,
      "degradation_cost_per_kwh": 0.2
    },
    "plan_snapshot": { "...": "原样回传 /dispatch 的完整响应（periods + summary）" },
    "settlement_tolerance_kwh": 0.01,
    "records": [
      {"base_load_kw": 0.2, "pv_kw": 0.0,
       "charge_ac_energy_kwh": 0.222222, "discharge_ac_energy_kwh": 0.0,
       "grid_purchase_energy_kwh": 0.322222, "soc_start_kwh": 0.0, "soc_end_kwh": 0.2},
      {"base_load_kw": 0.2, "pv_kw": 0.0,
       "charge_ac_energy_kwh": 0.0, "discharge_ac_energy_kwh": 0.09,
       "grid_purchase_energy_kwh": 0.015, "soc_start_kwh": 0.2, "soc_end_kwh": 0.1},
      {"base_load_kw": 0.2, "pv_kw": 0.0,
       "charge_ac_energy_kwh": 0.0, "discharge_ac_energy_kwh": 0.09,
       "grid_purchase_energy_kwh": 0.01, "soc_start_kwh": 0.1, "soc_end_kwh": 0.0},
      {"base_load_kw": 0.2, "pv_kw": 0.0,
       "charge_ac_energy_kwh": 0.0, "discharge_ac_energy_kwh": 0.0,
       "grid_purchase_energy_kwh": 0.1, "soc_start_kwh": 0.0, "soc_end_kwh": 0.0}
    ]
  }'
```

```json
{
  "num_records": 4,
  "period_hours": 0.5,
  "settlement_tolerance_kwh": 0.01,
  "status": "settled",
  "settled_periods": 4,
  "pending_periods": 0,
  "first_anomaly_index": null,
  "anomalous_records": [],
  "soc_breakpoint_records": [],
  "periods": [
    {
      "index": 1,
      "status": "settled",
      "plan": {
        "action": "discharge",
        "grid_purchase_energy_kwh": 0.01,
        "battery_throughput_kwh": 0.1,
        "energy_cost_yuan": 0.01,
        "degradation_cost_yuan": 0.02,
        "soc_start_kwh": 0.2,
        "soc_end_kwh": 0.1
      },
      "soc_gap_start_kwh": 0.0,
      "soc_gap_end_kwh": 0.0,
      "actual_grid_purchase_energy_kwh": 0.015,
      "actual_battery_throughput_kwh": 0.1,
      "actual_energy_cost_yuan": 0.015,
      "actual_degradation_cost_yuan": 0.02,
      "actual_total_cost_yuan": 0.035,
      "planned_energy_cost_yuan": 0.01,
      "planned_degradation_cost_yuan": 0.02,
      "planned_total_cost_yuan": 0.03,
      "energy_cost_delta_yuan": 0.005,
      "degradation_cost_delta_yuan": 0.0,
      "total_cost_delta_yuan": 0.005,
      "grid_purchase_delta_kwh": 0.005,
      "throughput_delta_kwh": 0.0,
      "audit": {
        "index": 1,
        "expected_soc_end_kwh": 0.1,
        "soc_residual_kwh": 0.0,
        "expected_grid_purchase_energy_kwh": 0.015,
        "grid_residual_kwh": 0.0,
        "soc_continuity_gap_kwh": 0.0,
        "anomalous": false,
        "reasons": [],
        "soc_breakpoint": false,
        "soc_breakpoint_reasons": []
      },
      "reasons": []
    }
  ],
  "summary": {
    "settled_periods": 4,
    "actual_grid_purchase_kwh": 0.447222,
    "planned_grid_purchase_kwh": 0.442222,
    "grid_purchase_delta_kwh": 0.005,
    "actual_battery_throughput_kwh": 0.4,
    "planned_battery_throughput_kwh": 0.4,
    "throughput_delta_kwh": 0.0,
    "actual_energy_cost_yuan": 0.025,
    "planned_energy_cost_yuan": 0.02,
    "energy_cost_delta_yuan": 0.005,
    "actual_degradation_cost_yuan": 0.08,
    "planned_degradation_cost_yuan": 0.08,
    "degradation_cost_delta_yuan": 0.0,
    "actual_total_cost_yuan": 0.105,
    "planned_total_cost_yuan": 0.1,
    "total_cost_delta_yuan": 0.005
  },
  "audit_summary": {"anomalous": false, "anomalous_records": []}
}
```

若 t2 实测购电量为 0.11（核算值 0.01，残差 0.1 超容差），则 t0/t1 正常结算、t2 起冻结：

```json
{
  "status": "partially_settled",
  "settled_periods": 2,
  "pending_periods": 2,
  "first_anomaly_index": 2,
  "anomalous_records": [2],
  "soc_breakpoint_records": [],
  "periods": [
    {"index": 0, "status": "settled", "...": "..."},
    {"index": 1, "status": "settled", "...": "..."},
    {
      "index": 2,
      "status": "pending",
      "actual_grid_purchase_energy_kwh": null,
      "actual_energy_cost_yuan": null,
      "total_cost_delta_yuan": null,
      "planned_total_cost_yuan": 0.03,
      "reasons": ["购电量残差 +0.100000kWh 超容差 0.01kWh（实测 0.11kWh，核算值 0.01kWh）"],
      "...": "审计分项残差与 SOC 偏差照常返回"
    },
    {
      "index": 3,
      "status": "pending",
      "actual_total_cost_yuan": null,
      "reasons": ["前序第 2 条记录已出现异常/SOC 断点，后续记录冻结为待结算"],
      "...": "..."
    }
  ],
  "summary": {
    "settled_periods": 2,
    "actual_total_cost_yuan": 0.07,
    "planned_total_cost_yuan": 0.07,
    "total_cost_delta_yuan": 0.0,
    "...": "汇总只覆盖已结算前缀；购电量、吞吐量、能量费、衰减费均给出实际/计划/差额三列"
  }
}
```

待结算记录的各 `actual_*` 与差额字段为 `null`，但计划口径（`planned_*`）、审计残差、SOC 偏差（`soc_gap_start_kwh` / `soc_gap_end_kwh`）与冻结原因照常返回，便于人工复核。

### `POST /dispatch/replan`

计划执行到**中途**时，依据连续实测状态续排余段。请求包含六样东西：

| 字段 | 说明 |
|---|---|
| `dispatch` | 生成原计划时使用的**完整 `/dispatch` 输入** |
| `plan_snapshot` | 当初 `/dispatch` 返回的计划快照（`periods` + `summary` 原样回传） |
| `audit_tolerance_kwh` | 审计容差（kWh，非负），规则与 `/telemetry/audit` 完全一致：残差绝对值**严格大于**它才判异常 |
| `executed_records` | 自原时间轴起点**连续已执行**的半小时实测记录（字段同 `/telemetry/audit`），至少一段、且必须**留有待执行时段**（段数 < 原计划时段数） |
| `remaining_prices` | 剩余时段的**新电价**预测，长度 = 原时段数 − 已执行段数 |
| `remaining_base_load_kw` / `remaining_pv_kw` | 剩余时段的**新负荷 / 光伏**预测，长度同上 |

服务端处理顺序：

1. **重算核对原计划**：用现有求解器对 `dispatch` 重算，逐字段比对 `plan_snapshot`（与结算同一套防篡改核对），不一致即拒绝；
2. **审计实测前缀**：按现有遥测审计规则核对连续前缀（电芯能量守恒、购电量平衡、相邻记录 SOC 衔接），并额外要求**首条记录的首态 SOC 在审计容差内接原初值**；
3. **保留实测、从末态重排**：已执行动作即使偏离旧计划，只要实测自洽就**保留原记录**（不做 SOC 断点判定），并从前缀末态重新安排余段；前缀末态 SOC 必须落在 **0.1kWh 网格**上，否则离散求解无法精确承接；
4. **末端约束核对**：原 `min_terminal_soc_kwh` 约束从实测末态起、在余段新预测下仍须可达，否则按无解拒绝；
5. **输出**：原时间轴上已执行（`executed`）与待执行（`planned`）的逐时段明细；按**现有结算口径**计算的实测前缀费用（实测购电量×电价 + 实测电芯吞吐量×衰减单价）加余段预测费用；以及可**直接交给 `/dispatch/settlement`** 的余段调度输入（`remaining_dispatch`，初始 SOC 已设为前缀末态）与余段计划快照（`remaining_plan_snapshot`）。

示例（前 2 段按原计划执行，余段 t2 电价从 1 元更新为 5 元）：

```bash
curl -s -X POST http://localhost:8000/dispatch/replan \
  -H 'Content-Type: application/json' \
  -d '{
    "dispatch": {
      "prices": [0.0, 1.0, 1.0, 0.0],
      "base_load_kw": [0.2, 0.2, 0.2, 0.2],
      "pv_kw": [0.0, 0.0, 0.0, 0.0],
      "capacity_kwh": 0.4,
      "max_charge_kw": 1.0,
      "max_discharge_kw": 1.0,
      "charge_efficiency": 0.9,
      "discharge_efficiency": 0.9,
      "initial_soc_kwh": 0.0,
      "min_terminal_soc_kwh": 0.0,
      "degradation_cost_per_kwh": 0.2
    },
    "plan_snapshot": { "...": "原样回传 /dispatch 的完整响应（periods + summary）" },
    "audit_tolerance_kwh": 0.01,
    "executed_records": [
      {"base_load_kw": 0.2, "pv_kw": 0.0,
       "charge_ac_energy_kwh": 0.222222, "discharge_ac_energy_kwh": 0.0,
       "grid_purchase_energy_kwh": 0.322222, "soc_start_kwh": 0.0, "soc_end_kwh": 0.2},
      {"base_load_kw": 0.2, "pv_kw": 0.0,
       "charge_ac_energy_kwh": 0.0, "discharge_ac_energy_kwh": 0.09,
       "grid_purchase_energy_kwh": 0.01, "soc_start_kwh": 0.2, "soc_end_kwh": 0.1}
    ],
    "remaining_prices": [5.0, 0.0],
    "remaining_base_load_kw": [0.2, 0.2],
    "remaining_pv_kw": [0.0, 0.0]
  }'
```

```json
{
  "num_periods": 4,
  "period_hours": 0.5,
  "audit_tolerance_kwh": 0.01,
  "executed_periods": 2,
  "remaining_periods": 2,
  "periods": [
    {
      "index": 0,
      "status": "executed",
      "record": {"...": "原始实测记录原样保留"},
      "plan": {
        "action": "charge",
        "grid_purchase_energy_kwh": 0.322222,
        "battery_throughput_kwh": 0.2,
        "energy_cost_yuan": 0.0,
        "degradation_cost_yuan": 0.04,
        "soc_start_kwh": 0.0,
        "soc_end_kwh": 0.2
      },
      "soc_gap_start_kwh": 0.0,
      "soc_gap_end_kwh": 0.0,
      "actual_grid_purchase_energy_kwh": 0.322222,
      "actual_battery_throughput_kwh": 0.2,
      "actual_energy_cost_yuan": 0.0,
      "actual_degradation_cost_yuan": 0.04,
      "actual_total_cost_yuan": 0.04,
      "planned_energy_cost_yuan": 0.0,
      "planned_degradation_cost_yuan": 0.04,
      "planned_total_cost_yuan": 0.04,
      "energy_cost_delta_yuan": 0.0,
      "degradation_cost_delta_yuan": 0.0,
      "total_cost_delta_yuan": 0.0,
      "grid_purchase_delta_kwh": 0.0,
      "throughput_delta_kwh": 0.0,
      "audit": {"index": 0, "anomalous": false, "reasons": [], "...": "三类分项残差同 /telemetry/audit"},
      "reasons": []
    },
    {"index": 1, "status": "executed", "...": "同结构；实测自洽即通过，对旧计划的偏离只体现在 soc_gap_* 与各 delta 中"},
    {
      "index": 2,
      "status": "planned",
      "remaining_index": 0,
      "action": "discharge",
      "action_energy_kwh": -0.1,
      "charge_ac_energy_kwh": 0.0,
      "discharge_ac_energy_kwh": 0.09,
      "battery_power_kw": -0.18,
      "grid_purchase_energy_kwh": 0.01,
      "pv_curtailed_energy_kwh": 0.0,
      "soc_start_kwh": 0.1,
      "soc_end_kwh": 0.0,
      "energy_cost_yuan": 0.05,
      "degradation_cost_yuan": 0.02
    },
    {"index": 3, "status": "planned", "remaining_index": 1, "action": "idle", "...": "..."}
  ],
  "executed_summary": {
    "executed_periods": 2,
    "final_soc_kwh": 0.1,
    "actual_grid_purchase_kwh": 0.332222,
    "planned_grid_purchase_kwh": 0.332222,
    "grid_purchase_delta_kwh": 0.0,
    "actual_battery_throughput_kwh": 0.3,
    "planned_battery_throughput_kwh": 0.3,
    "throughput_delta_kwh": 0.0,
    "actual_energy_cost_yuan": 0.01,
    "planned_energy_cost_yuan": 0.01,
    "energy_cost_delta_yuan": 0.0,
    "actual_degradation_cost_yuan": 0.06,
    "planned_degradation_cost_yuan": 0.06,
    "degradation_cost_delta_yuan": 0.0,
    "actual_total_cost_yuan": 0.07,
    "planned_total_cost_yuan": 0.07,
    "total_cost_delta_yuan": 0.0
  },
  "remaining_summary": { "...": "余段计划的 summary，口径同 /dispatch（num_periods=2）" },
  "actual_prefix_total_cost_yuan": 0.07,
  "remaining_forecast_total_cost_yuan": 0.07,
  "total_cost_yuan": 0.14,
  "remaining_dispatch": {
    "prices": [5.0, 0.0],
    "base_load_kw": [0.2, 0.2],
    "pv_kw": [0.0, 0.0],
    "capacity_kwh": 0.4,
    "max_charge_kw": 1.0,
    "max_discharge_kw": 1.0,
    "charge_efficiency": 0.9,
    "discharge_efficiency": 0.9,
    "initial_soc_kwh": 0.1,
    "min_terminal_soc_kwh": 0.0,
    "degradation_cost_per_kwh": 0.2
  },
  "remaining_plan_snapshot": { "...": "余段计划（periods + summary），与 remaining_dispatch 配套" }
}
```

要点：

- `periods` 按**原时间轴下标**升序混合返回：已执行段 `status=executed`（含原始记录、原计划回显、实测费用、与原计划的差额、审计残差；偏离旧计划不是异常），待执行段 `status=planned`（字段同 `/dispatch` 的逐时段计划，另带余段内相对下标 `remaining_index`）；
- `remaining_dispatch.initial_soc_kwh` 等于前缀末态 SOC（`executed_summary.final_soc_kwh`），余段首时段 `soc_start_kwh` 与之**严格衔接**；末端 SOC 约束、容量、效率、功率、衰减单价均沿用原输入；
- 费用三段并列：`actual_prefix_total_cost_yuan`（前缀实测）、`remaining_forecast_total_cost_yuan`（余段按新电价/负荷/光伏的预测）、`total_cost_yuan`（两者之和）。

余段执行完后，把 `remaining_dispatch`、`remaining_plan_snapshot` 与余段实测记录一起 POST 给 `/dispatch/settlement` 即可完成余段结算（快照可通过重算核对）。

整体拒绝（HTTP 422）的情形：

- `plan_snapshot_mismatch`：原计划快照与重算不一致；原输入重算无解时为 `infeasible`；
- `prefix_audit_failed`：首条记录首态 SOC 未在容差内接原初值，或前缀中任一条未通过遥测审计；
- `prefix_soc_off_grid`：前缀末态 SOC 不落在 0.1kWh 网格（或超出容量）；
- `infeasible`：从实测末态出发，原末端 SOC 约束在余段新预测下已无法满足；
- `invalid_input`：前缀为空 / 覆盖全周期、余段预测长度与剩余时段数不符、容差或测量值非法等。

### `POST /dispatch/sessions` — 创建执行会话

实测记录常**分批到达**（每批覆盖若干连续的半小时时段）。先创建会话锁定计划基线：

| 字段 | 说明 |
|---|---|
| `dispatch` | 生成原计划时使用的**完整 `/dispatch` 输入** |
| `plan_snapshot` | 当初 `/dispatch` 返回的完整计划快照（`periods` + `summary`） |
| `audit_tolerance_kwh` | 审计容差（kWh，非负），之后每批追加都沿用它；残差绝对值**严格大于**它即判异常 |

服务端**先重算并逐字段核对快照**（与结算、续排同一套防篡改核对），不一致返回 `plan_snapshot_mismatch`、原输入重算无解返回 `infeasible`，均不创建会话；核对通过后把原输入、快照与容差持久化，返回 `session_id`（UUID）、`num_periods`、`next_index=0`、`status=awaiting_records`。

### `POST /dispatch/sessions/{session_id}/batches` — 成批追加实测记录

请求体形如 `{"start_index": 0, "records": [ …半小时实测记录… ]}`，记录字段同 `/telemetry/audit`，每批 1–200 条：

1. **下标衔接**：`start_index` 必须等于会话当前 `next_index`（首批为 0），下标跳跃（`batch_index_gap`）或区间超出 `[0, num_periods)`（`batch_out_of_range`）都**整批不入库**；
2. **首态衔接**：首批首态 SOC 须在容差内接**原初始 SOC**；后续批次首态 SOC 须在容差内接**已保存最后一条的末态 SOC**（`batch_boundary_gap` 整批不入库）；
3. **逐段审计**：沿用现有 `audit_telemetry` 核对批内每条记录的电芯能量守恒、购电量平衡与相邻记录 SOC 衔接；实测动作**偏离计划但自洽时仍接受**（会话不做计划 SOC 轨迹核对，偏离不是异常）；任一条异常（`batch_audit_failed`）都**整批不入库**，原因中点名首个异常下标与残差；
4. **原子入库**：上述检查全部通过后，整批记录、批次边界（`start_index` + 批长）与**首次成功回执**在同一 SQLite 写事务内确认，返回 `result=accepted`、更新后的 `confirmed_periods` / `next_index` / `status`（`awaiting_records` → `in_progress` → `complete`）、本批逐条 `batch_audit`、`first_record_soc_gap_kwh` 与累计 `final_soc_kwh`；
5. **重试与冲突**：重试判定以持久化的批次边界为准——**起点、长度、内容三者都与某个已确认整批一致**才算原样重试 → HTTP 200、`result=deduped`，**除 `result` 外其余字段保持该批首次成功时的值**（`confirmed_periods` / `next_index` / `status` / `final_soc_kwh` 等不随后续批次入库而漂移），且不重复入库；**同起点短批、内容变更、跨批拼接**，或落在已确认区间内却没有匹配批次边界的上报（含并发上报）→ **HTTP 409 `conflict`**，已保存记录与进度不变，响应原因中给出当前 `next_index`。跨进程并发、进程重启、后续批次入库后的重试都遵守同一套规则；旧库只有逐段记录、无法确认历史批次边界时，查询与从 `next_index` 续报照常可用，但重叠上报一律 409，**不按前缀猜测去重**。

### `GET /dispatch/sessions/{session_id}` 与 `GET /dispatch/sessions`

单会话查询返回：原计划信息（`dispatch` + `plan_snapshot` + `audit_tolerance_kwh`）、自下标 0 起**连续已确认记录** `confirmed_records`（每条带全局 `index`、原始 `record` 与 `stored_at`）、对全部确认记录重算的 `confirmed_audit`、`next_index`、`status`、`initial_soc_kwh` 与 `final_soc_kwh`。未完成的前缀可直接取 `confirmed_records[*].record` 作为 `executed_records` 交给 `/dispatch/replan`；`status=complete` 的满期记录可直接作为 `records` 交给 `/dispatch/settlement`。列表接口返回全部会话的概览（确认段数、下一下标、状态、首末 SOC、时间戳）。未知会话返回 **404 `session_not_found`**。

### `POST /dispatch/sessions/{session_id}/revisions` — 计量表更正

会话**满期（`status=complete`）后**，如计量表事后被校表更正，可在保留最初上报证据的同时叠加一份修订后的实测台账。请求体：

| 字段 | 说明 |
|---|---|
| `expected_revision` | 客户端所基于的**当前修订号**（乐观并发）：完成会话从 **0** 开始，每次成功更正 +1；与服务端当前号不符返回 **409** 及当前号 |
| `changes` | **至少一个**替换项，每项为 `{"index": 时段下标, "record": 完整半小时实测记录}`；记录字段同 `/telemetry/audit`，是**整条完整替换**（不是字段级 patch，七个字段都要给出），一次最多 200 项 |

服务端处理顺序：

1. **前置校验**：会话必须满期（`revision_not_complete`）；同一次提交内下标**重复**（`revision_duplicate_index`）或下标**越界** `[0, num_periods)`（`revision_invalid_index`）、替换记录负值/非有限/SOC 超容量（`invalid_input`）都**整次拒绝**，原因中点名下标；
2. **修订号核对**：在 SQLite 写事务内取最新修订号，`expected_revision` 不等于当前号 → **HTTP 409 `revision_conflict`**，错误体顶层另带 `current_revision`，本次不写入；
3. **当前版本上叠加**：修订号 0 的基线是最初上报记录；之后取最新修订台账，仅替换本次给出的下标，**未指定时段保持原值**；
4. **整段重新核对**：用会话自己的**容量、充/放效率、初始 SOC 与审计容差**，对叠加后的**整段**记录重核——下标 0 的首态 SOC 须在容差内接原初始 SOC（`revision_anchor_gap`），并沿用现有 `audit_telemetry` 逐段核对电芯能量守恒、购电量平衡与相邻记录 SOC 衔接；**任一时段**能量失衡或相邻 SOC 断点 → `revision_audit_failed` **整次拒绝，已有修订不变**，原因点名首个异常下标与残差；
5. **原子落盘**：核对通过后，新修订号、本次原始变更与叠加后的整段修订台账在**同一写事务**内保存；随后响应同查询接口。

更正**只写独立的修订表**：`session_records`（最初上报证据）、`session_batches`（批次边界、首次回执）永不被改写——原会话查询、批次重试判定（仍是最初上报内容的 `deduped`/`conflict`）与首收回执冻结值都继续使用最初上报记录。

### `GET /dispatch/sessions/{session_id}/revisions` — 查询原始与修订台账

满期会话返回：

| 字段 | 说明 |
|---|---|
| `current_revision` | 当前修订号（未更正为 0） |
| `original_records` | **最初上报**的完整台账（每条带 `index`、原始 `record`、`stored_at`），任何更正都不改写 |
| `revised_records` | **当前修订后**的整段台账（未指定替换的时段保持原值） |
| `changed_indices` | 当前修订台账相对**最初上报**逐字段有差异的下标（升序；改回原值的下标自动移出） |
| `revisions` | 历次更正历史：`revision_no`、该次**原始提交**的 `changes`、该次涉及的 `changed_indices` 与 `created_at` |
| `revised_audit` | 用会话容量/效率/初始 SOC/容差对整段修订台账重算的逐条遥测审计（全局下标） |
| `initial_soc_kwh` / `final_soc_kwh` | 原初始 SOC 与当前修订台账末条末态 SOC |
| `settlement_request` | **可直接整体 POST 给 `/dispatch/settlement` 的完整请求数据**：`{dispatch, plan_snapshot, settlement_tolerance_kwh, records}`，其中容差取会话审计容差、`records` 为当前修订台账 |

未满期会话查询/提交更正均返回 422 `revision_not_complete`；未知会话 404。修订与原始记录同在命名卷 SQLite 中，**进程重启后仍可查询，并可在当前修订号上继续更正**。

更正接口的错误码（除 `revision_conflict` 为 **HTTP 409**、`session_not_found` 为 **HTTP 404** 外，其余为 422）：

- `revision_not_complete`：会话未满期（未确认完全部时段），不允许更正/查询修订；
- `revision_invalid_index`：替换下标越界（合法范围 `[0, num_periods)`），原因点名下标；
- `revision_duplicate_index`：同一次提交内同一下标重复出现；
- `revision_anchor_gap`：修订台账下标 0 首态 SOC 未在容差内接原初始 SOC；
- `revision_audit_failed`：整段重核中某时段电芯能量/购电量失衡或与相邻记录 SOC 断点，原因点名首个异常下标；
- `revision_conflict`（409）：期望修订号过期，错误体附当前号 `current_revision`；
- `session_not_found`（404）：会话 ID 不存在；
- `invalid_input`：期望修订号为负、`changes` 为空/超长、替换记录测量值为负/非有限、SOC 超容量等。

### 可执行示例（计量表更正、查询、结算）

下例沿用前面创建的 4 段满期会话（`SID`、容差 0.01kWh）。更正 t1 的计量表读数：实测基础负荷 0.22kW（原上报 0.2），购电量同步更正为 0.02kWh（SOC 与充放电轨迹不变）。

```bash
BASE=http://localhost:8000

# 1) 首次更正：期望修订号 0（完成会话的起始号），整段重核通过后落盘为修订号 1
cat > /tmp/rev1.json <<'JSON'
{"expected_revision": 0, "changes": [
  {"index": 1, "record":
    {"base_load_kw": 0.22, "pv_kw": 0.0,
     "charge_ac_energy_kwh": 0.0, "discharge_ac_energy_kwh": 0.09,
     "grid_purchase_energy_kwh": 0.02, "soc_start_kwh": 0.2, "soc_end_kwh": 0.1}}
]}
JSON
curl -s -X POST $BASE/dispatch/sessions/$SID/revisions \
  -H 'Content-Type: application/json' -d @/tmp/rev1.json \
  | tee /tmp/rev1-resp.json | jq \
    '{current_revision, changed_indices,
      original_t1: .original_records[1].record.grid_purchase_energy_kwh,
      revised_t1: .revised_records[1].record.grid_purchase_energy_kwh,
      final_soc_kwh}'
# => {"current_revision": 1, "changed_indices": [1],
#     "original_t1": 0.01, "revised_t1": 0.02, "final_soc_kwh": 0.0}

# 2) 修订号过期：仍带 0 提交 → HTTP 409，错误体给出当前号
curl -s -o /tmp/stale.json -w '%{http_code}\n' \
  -X POST $BASE/dispatch/sessions/$SID/revisions \
  -H 'Content-Type: application/json' -d @/tmp/rev1.json
jq '{error, current_revision}' /tmp/stale.json   # {"error":"revision_conflict","current_revision":1}

#    下标越界 / 同一提交内下标重复 → 422 整次拒绝，原因点名下标
jq -c '.expected_revision=1 | .changes[0].index=4' /tmp/rev1.json \
  | curl -s -X POST $BASE/dispatch/sessions/$SID/revisions \
      -H 'Content-Type: application/json' -d @- | jq .error   # "revision_invalid_index"

#    整段能量失衡（只改 t2 首态而不修衔接链，造成 t1→t2 SOC 断点）→ 422，已有修订不变
jq -c '.expected_revision=1 | .changes=[{"index":2,"record":
  (.revised_records[2].record | .soc_start_kwh=0.15 | .soc_end_kwh=0.05)}]' \
  /tmp/rev1-resp.json \
  | curl -s -X POST $BASE/dispatch/sessions/$SID/revisions \
      -H 'Content-Type: application/json' -d @- | jq '{error, reason}'
# error="revision_audit_failed"，reason 点名「下标 2 … SOC 不衔接」；修订号仍为 1

# 3) 后续更正在当前版本上叠加（期望修订号 1），未指定时段保持现值
cat > /tmp/rev2.json <<'JSON'
{"expected_revision": 1, "changes": [
  {"index": 1, "record":
    {"base_load_kw": 0.2, "pv_kw": 0.0,
     "charge_ac_energy_kwh": 0.0, "discharge_ac_energy_kwh": 0.09,
     "grid_purchase_energy_kwh": 0.01, "soc_start_kwh": 0.2, "soc_end_kwh": 0.1}}
]}
JSON
curl -s -X POST $BASE/dispatch/sessions/$SID/revisions \
  -H 'Content-Type: application/json' -d @/tmp/rev2.json \
  | jq '{current_revision, changed_indices, revisions: [.revisions[].revision_no]}'
# 把 t1 改回原值后：{"current_revision":2,"changed_indices":[],"revisions":[1,2]}

# 4) 查询：原始证据 + 当前修订台账 + 差异下标 + 历次更正 + 可直接结算的完整请求
curl -s $BASE/dispatch/sessions/$SID/revisions | tee /tmp/rev-detail.json | jq \
  '{current_revision, changed_indices,
    original_grid_t1: .original_records[1].record.grid_purchase_energy_kwh,
    revised_grid_t1: .revised_records[1].record.grid_purchase_energy_kwh}'

# 5) 直接把 settlement_request 整体提交给现有结算接口（容差取会话审计容差）
jq -c '.settlement_request' /tmp/rev-detail.json \
  | curl -s -X POST $BASE/dispatch/settlement -H 'Content-Type: application/json' -d @- \
  | jq '{status, settled_periods, actual_total_cost: .summary.actual_total_cost_yuan}'

# 6) 更正不改写最初上报：原会话查询与批次重试仍是最初记录 / deduped 首收回执
curl -s $BASE/dispatch/sessions/$SID | jq '{current_revision, confirmed_t1: .confirmed_records[1].record.grid_purchase_energy_kwh}'
# => {"current_revision":2,"confirmed_t1":0.01}
curl -s -X POST $BASE/dispatch/sessions/$SID/batches -H 'Content-Type:application/json' \
  -d @/tmp/batch0.json | jq '{result, final_soc_kwh}'   # {"result":"deduped","final_soc_kwh":0.1}
```

重启验证：`docker compose restart`（命名卷保留）后再次执行第 4 步，修订历史与当前台账仍在；继续更正须带查询到的 `current_revision`，带旧号提交返回 409 与当前号。

### `POST /dispatch/sessions/{session_id}/vouchers` — 出具逐版结算凭证

满期会话可按修订号出具**可追溯的逐版结算凭证**。请求体只有一个字段：`{"expected_revision": 修订号}`。

- **修订号 0**：对**最初上报记录**结算；**修订号 n≥1**：对**修订号 n 的更正台账**（每次更正成功时整段冻结落盘）结算；
- 核算完全**沿用现有结算规则**：原计划重算核对 → 遥测审计 → SOC 轨迹核对 → 计费，即凭证冻结的 `settlement` 与直接 POST `/dispatch/settlement` 的结果逐字段一致；
- **只能结算有效前缀**（首条即异常的 `no_valid_interval`，或从某条起异常 / SOC 断点的 `voucher_partial_settled`）时**整次拒绝，不留下任何凭证**；计量表更正在提交时已保证整段遥测自洽，但不核对计划 SOC 轨迹，因此「自洽却偏离计划」的修订会在此被拦下，可再提交一次修复台账的更正后对新修订号出单；
- 凭证保存**修订号、结算摘要、实测总费用、出单时间**，并冻结出单时该版**完整台账与完整结算报告**（`records` / `settlement`）；
- **计费栏 `billing`**：本会话**首张凭证记全额**（`type=full`）；其后每张列出**相对上一张凭证**的差额——总费用升高为应补（`supplement`）、降低为应退（`refund`）、相等为 `no_adjustment`；差额总是相对**最近一张已出凭证**，因此**允许跳过未出单的中间修订**（`relative_to_revision` / `relative_to_voucher_no` 指明对比的是哪一张）；
- **幂等**：同一修订号重试，返回落盘时冻结的**原凭证**（`result=existing`，`voucher_id` / `issued_at` / `billing` / 台账 / 结算报告全部不变），**即使后来又有更新的修订**；
- **冲突**：请求修订号**未出单且不是当前修订号**（漏开的过期版本或超前版本）→ **HTTP 409 `voucher_revision_conflict`**，错误体附 `current_revision`，不写入；
- 出单与计量表更正共用同一把 SQLite 写事务锁：「取当前号 → 取该版台账 → 核算 → 落盘」整段原子，并发更正不可能插到中间使凭证引用过期台账；核算失败一律回滚，**不留下部分数据**。

### `GET /dispatch/sessions/{session_id}/vouchers` — 按出单次序查询凭证与待出单状态

| 字段 | 说明 |
|---|---|
| `current_revision` | 会话当前修订号（未更正为 0） |
| `pending.current_revision` | 同上（待出单状态对象内冗余一份） |
| `pending.voucher_revisions` | **已出单的修订号**，按出单次序排列（允许不连续，如 `[0, 3]`） |
| `pending.next_revision_settled` | 当前修订号是否已有凭证：`false` 即当前修订尚待出单 |
| `vouchers` | 全部已出凭证，按**出单次序**（`voucher_no` 升序），每张均为落盘时冻结的内容：`voucher_id`、`revision_no`、出单当时的 `current_revision`、`total_cost_yuan`、`billing`、`issued_at`、冻结的 `records` 与完整 `settlement` |

凭证与会话同在命名卷 SQLite 中，**进程重启后仍可按出单次序查询**，且同修订号重试仍取回原凭证。未满期会话出单 / 查询均返回 422 `voucher_not_complete`；未知会话 404。

凭证接口的错误码（`voucher_revision_conflict` 为 **HTTP 409**、`session_not_found` 为 **HTTP 404**，其余为 422）：

- `voucher_not_complete`：会话未满期，不允许出单 / 查询凭证；
- `voucher_partial_settled`：该版台账只能结算有效前缀（错误体附 `first_anomaly_index` 与 `settled_periods`），整次拒绝、不留凭证；
- `voucher_revision_conflict`（409）：请求修订号未出单且不是当前修订号（漏开的过期版本或超前版本），错误体附 `current_revision`；
- `no_valid_interval` / `plan_snapshot_mismatch` 等：沿用 `/dispatch/settlement` 的整体拒绝错误码，同样不留下凭证；
- `session_not_found`（404）：会话 ID 不存在；
- `invalid_input`：期望修订号为负等请求体非法。

### 可执行示例（出单、修订后补退、跳过与查询）

下例独立创建一个 4 段满期会话（t0 谷时充 0.2kWh、t1/t2 各放 0.1kWh、t3 静置；效率 0.9、容差 0.01kWh），最初上报口径实测总费用 **0.10 元**。需要 `jq`，先 `docker compose up -d --build`。

```bash
BASE=http://localhost:8000

# 0) 创建会话并追加为满期（计划由服务端重算核对）
cat > /tmp/v-dispatch.json <<'JSON'
{
  "prices": [0.0, 1.0, 1.0, 0.0],
  "base_load_kw": [0.2, 0.2, 0.2, 0.2],
  "pv_kw": [0.0, 0.0, 0.0, 0.0],
  "capacity_kwh": 0.4,
  "max_charge_kw": 1.0,
  "max_discharge_kw": 1.0,
  "charge_efficiency": 0.9,
  "discharge_efficiency": 0.9,
  "initial_soc_kwh": 0.0,
  "min_terminal_soc_kwh": 0.0,
  "degradation_cost_per_kwh": 0.2
}
JSON
curl -s -X POST $BASE/dispatch -H 'Content-Type: application/json' \
  -d @/tmp/v-dispatch.json | jq -c . > /tmp/v-plan.json
VSID=$(curl -s -X POST $BASE/dispatch/sessions -H 'Content-Type: application/json' \
  -d "$(jq -n --slurpfile d /tmp/v-dispatch.json --slurpfile p /tmp/v-plan.json \
        '{dispatch:$d[0], plan_snapshot:$p[0], audit_tolerance_kwh:0.01}')" \
  | jq -r .session_id)
cat > /tmp/v-batch0.json <<'JSON'
{"start_index": 0, "records": [
  {"base_load_kw": 0.2, "pv_kw": 0.0,
   "charge_ac_energy_kwh": 0.222222, "discharge_ac_energy_kwh": 0.0,
   "grid_purchase_energy_kwh": 0.322222, "soc_start_kwh": 0.0, "soc_end_kwh": 0.2},
  {"base_load_kw": 0.2, "pv_kw": 0.0,
   "charge_ac_energy_kwh": 0.0, "discharge_ac_energy_kwh": 0.09,
   "grid_purchase_energy_kwh": 0.01, "soc_start_kwh": 0.2, "soc_end_kwh": 0.1}
]}
JSON
cat > /tmp/v-batch2.json <<'JSON'
{"start_index": 2, "records": [
  {"base_load_kw": 0.2, "pv_kw": 0.0,
   "charge_ac_energy_kwh": 0.0, "discharge_ac_energy_kwh": 0.09,
   "grid_purchase_energy_kwh": 0.01, "soc_start_kwh": 0.1, "soc_end_kwh": 0.0},
  {"base_load_kw": 0.2, "pv_kw": 0.0,
   "charge_ac_energy_kwh": 0.0, "discharge_ac_energy_kwh": 0.0,
   "grid_purchase_energy_kwh": 0.1, "soc_start_kwh": 0.0, "soc_end_kwh": 0.0}
]}
JSON
curl -s -X POST $BASE/dispatch/sessions/$VSID/batches -H 'Content-Type: application/json' \
  -d @/tmp/v-batch0.json | jq '{result, status}'
curl -s -X POST $BASE/dispatch/sessions/$VSID/batches -H 'Content-Type: application/json' \
  -d @/tmp/v-batch2.json | jq '{result, status}'

# 1) 首张凭证：修订号 0（最初上报记录），计费栏记全额 0.10 元
curl -s -X POST $BASE/dispatch/sessions/$VSID/vouchers \
  -H 'Content-Type: application/json' -d '{"expected_revision": 0}' \
  | tee /tmp/v-voucher0.json | jq \
    '{voucher_no, result, revision_no, total_cost_yuan, billing, issued_at}'
# => {"voucher_no":1,"result":"issued","revision_no":0,"total_cost_yuan":0.1,
#     "billing":{"type":"full","amount_yuan":0.1,"relative_to_revision":null,
#                "relative_to_voucher_no":null},"issued_at":"...Z"}

# 2) 计量表更正：t1 实际负荷 0.22kW、购电量 0.02kWh（SOC/充放电轨迹不变），
#    该时段多购 0.01kWh × 1 元 = 多 0.01 元
cat > /tmp/v-rev1.json <<'JSON'
{"expected_revision": 0, "changes": [
  {"index": 1, "record":
    {"base_load_kw": 0.22, "pv_kw": 0.0,
     "charge_ac_energy_kwh": 0.0, "discharge_ac_energy_kwh": 0.09,
     "grid_purchase_energy_kwh": 0.02, "soc_start_kwh": 0.2, "soc_end_kwh": 0.1}}
]}
JSON
curl -s -X POST $BASE/dispatch/sessions/$VSID/revisions \
  -H 'Content-Type: application/json' -d @/tmp/v-rev1.json | jq '{current_revision}'

# 3) 对修订 1 出单：相对上一张（修订 0）应补 0.01 元
curl -s -X POST $BASE/dispatch/sessions/$VSID/vouchers \
  -H 'Content-Type: application/json' -d '{"expected_revision": 1}' \
  | tee /tmp/v-voucher1.json | jq \
    '{voucher_no, revision_no, total_cost_yuan, billing}'
# => {"voucher_no":2,"revision_no":1,"total_cost_yuan":0.11,
#     "billing":{"type":"supplement","amount_yuan":0.01,
#                "relative_to_revision":0,"relative_to_voucher_no":1}}

# 4) 同一修订号重试：返回原凭证（result=existing），即使修订继续前进也不变
curl -s -X POST $BASE/dispatch/sessions/$VSID/vouchers \
  -H 'Content-Type: application/json' -d '{"expected_revision": 0}' \
  | jq --slurpfile orig /tmp/v-voucher0.json \
    '{result, total_cost_yuan, billing, same_voucher: (.voucher_id == $orig[0].voucher_id)}'
# => {"result":"existing","total_cost_yuan":0.1,
#     "billing":{"type":"full", ...},"same_voucher":true}

# 5) 再更正（修订 2）后，漏开的修订 1 已出过单；演示过期未出单冲突：
#    先推进到修订 3 且修订 2 不出单——把 t1 改到负荷 0.18（期望购电 0，总费用 0.09）
cat > /tmp/v-rev2.json <<'JSON'
{"expected_revision": 1, "changes": [
  {"index": 1, "record":
    {"base_load_kw": 0.24, "pv_kw": 0.0,
     "charge_ac_energy_kwh": 0.0, "discharge_ac_energy_kwh": 0.09,
     "grid_purchase_energy_kwh": 0.03, "soc_start_kwh": 0.2, "soc_end_kwh": 0.1}}
]}
JSON
curl -s -X POST $BASE/dispatch/sessions/$VSID/revisions \
  -H 'Content-Type: application/json' -d @/tmp/v-rev2.json | jq '{current_revision}'  # => 2
cat > /tmp/v-rev3.json <<'JSON'
{"expected_revision": 2, "changes": [
  {"index": 1, "record":
    {"base_load_kw": 0.18, "pv_kw": 0.0,
     "charge_ac_energy_kwh": 0.0, "discharge_ac_energy_kwh": 0.09,
     "grid_purchase_energy_kwh": 0.0, "soc_start_kwh": 0.2, "soc_end_kwh": 0.1}}
]}
JSON
curl -s -X POST $BASE/dispatch/sessions/$VSID/revisions \
  -H 'Content-Type: application/json' -d @/tmp/v-rev3.json | jq '{current_revision}'  # => 3

#    对未出单的过期修订 2 补开 → 409 冲突并给出当前号，不写入
curl -s -X POST $BASE/dispatch/sessions/$VSID/vouchers \
  -H 'Content-Type: application/json' -d '{"expected_revision": 2}' | jq '{error, current_revision}'
# => {"error":"voucher_revision_conflict","current_revision":3}

# 6) 跳过未出单的修订 2，直接对当前修订 3 出单：
#    差额相对最近一张已出凭证（修订 1 的 0.11）→ 应退 0.02 元
curl -s -X POST $BASE/dispatch/sessions/$VSID/vouchers \
  -H 'Content-Type: application/json' -d '{"expected_revision": 3}' \
  | jq '{voucher_no, revision_no, total_cost_yuan, billing}'
# => {"voucher_no":3,"revision_no":3,"total_cost_yuan":0.09,
#     "billing":{"type":"refund","amount_yuan":0.02,
#                "relative_to_revision":1,"relative_to_voucher_no":2}}

# 7) 按出单次序查询全部凭证 + 当前修订待出单状态
curl -s $BASE/dispatch/sessions/$VSID/vouchers | jq \
  '{current_revision, pending,
    vouchers: [.vouchers[] | {voucher_no, revision_no, total_cost_yuan, billing}]}'
# => {"current_revision":3,
#     "pending":{"current_revision":3,"voucher_revisions":[0,1,3],
#                "next_revision_settled":true},
#     "vouchers":[{voucher_no:1, revision_no:0, total_cost_yuan:0.1,  billing:{type:"full",…}},
#                 {voucher_no:2, revision_no:1, total_cost_yuan:0.11, billing:{type:"supplement",…}},
#                 {voucher_no:3, revision_no:3, total_cost_yuan:0.09, billing:{type:"refund",…}}]}

#    单张凭证里还带有冻结的完整结算报告（口径同 /dispatch/settlement）与该版台账
curl -s $BASE/dispatch/sessions/$VSID/vouchers > /tmp/v-list.json
jq '.vouchers[1] | {revision_no,
  settlement_status: .settlement.status,
  settled_periods: .settlement.settled_periods,
  t1_actual_grid: .settlement.periods[1].actual_grid_purchase_energy_kwh,
  frozen_t1_record: .records[1].record}' /tmp/v-list.json
```

只能结算有效前缀时整次拒绝且不留凭证（修订台账遥测自洽、但 SOC 轨迹偏离原计划）：

```bash
# 另建一个满期会话 VSID2，提交「t1 只放 0.05kWh、后续 SOC 链自洽承接」的更正
# （构造略，见修订接口示例）；该台账遥测审计通过，但 t1 起 SOC 偏离计划轨迹
curl -s -X POST $BASE/dispatch/sessions/$VSID2/vouchers \
  -H 'Content-Type: application/json' -d '{"expected_revision": 1}' \
  | jq '{error, first_anomaly_index, settled_periods}'
# => {"error":"voucher_partial_settled","first_anomaly_index":1,"settled_periods":1}
curl -s $BASE/dispatch/sessions/$VSID2/vouchers | jq '.vouchers | length'   # => 0（未留凭证）
# 再提交一次把台账修复回计划轨迹的修订（修订号 2）后，即可对修订 2 正常出单
```

重启验证：`docker compose restart`（命名卷保留）后再次执行第 7 步，凭证次序与冻结内容仍在；对修订 0/1/3 重试仍分别返回各自的原凭证（`result=existing`）。

### 持久化

会话、逐段记录、批次边界、首次回执、计量表修订与逐版结算凭证存放在容器内 `/data/sessions.db`（SQLite，环境变量 `DISPATCH_SESSION_DB` 可覆盖路径），Compose 已把命名卷 `dispatch-session-data` 挂到 `/data`：容器重建、镜像升级或进程重启后，会话、已确认记录、历次修订与已出凭证**原样保留**，可继续查询、幂等重试、续报、在当前修订号上继续更正，并可按出单次序取回冻结凭证。裸机运行时若 `/data` 不可写，自动落到工作目录 `./data/sessions.db`。旧版本库（只有逐段记录、无批次边界 / 修订 / 凭证表）打开时自动补建新表：查询与从 `next_index` 续报不受影响，历史区间因无法确认批次边界，重叠上报按 409 处理而不猜测去重；修订表为空时修订查询返回修订号 0（台账即最初上报记录）；凭证表为空时凭证查询返回空列表与当前修订待出单状态。

### 可执行示例（创建、分批追加、重试、查询、续排/结算）

以下示例在仓库目录直接可跑（需要 `jq`；先 `docker compose up -d --build`）。所用 4 段计划为「t0 谷时充 0.2kWh、t1/t2 各放 0.1kWh、t3 静置」，效率 0.9、容差 0.01kWh。

```bash
BASE=http://localhost:8000

# 0) 生成计划并保存输入与快照（jq -c 压成单行 JSON）
cat > /tmp/dispatch.json <<'JSON'
{
  "prices": [0.0, 1.0, 1.0, 0.0],
  "base_load_kw": [0.2, 0.2, 0.2, 0.2],
  "pv_kw": [0.0, 0.0, 0.0, 0.0],
  "capacity_kwh": 0.4,
  "max_charge_kw": 1.0,
  "max_discharge_kw": 1.0,
  "charge_efficiency": 0.9,
  "discharge_efficiency": 0.9,
  "initial_soc_kwh": 0.0,
  "min_terminal_soc_kwh": 0.0,
  "degradation_cost_per_kwh": 0.2
}
JSON
curl -s -X POST $BASE/dispatch -H 'Content-Type: application/json' \
  -d @/tmp/dispatch.json | jq -c . > /tmp/plan.json

# 1) 创建执行会话（服务端重算确认快照一致）
curl -s -X POST $BASE/dispatch/sessions -H 'Content-Type: application/json' \
  -d "$(jq -n --slurpfile d /tmp/dispatch.json --slurpfile p /tmp/plan.json \
        '{dispatch:$d[0], plan_snapshot:$p[0], audit_tolerance_kwh:0.01}')" \
  | tee /tmp/session.json | jq .
SID=$(jq -r .session_id /tmp/session.json)

# 2) 首批追加 t0、t1 两段（start_index=0，首态接原初始 SOC 0.0）
cat > /tmp/batch0.json <<'JSON'
{"start_index": 0, "records": [
  {"base_load_kw": 0.2, "pv_kw": 0.0,
   "charge_ac_energy_kwh": 0.222222, "discharge_ac_energy_kwh": 0.0,
   "grid_purchase_energy_kwh": 0.322222, "soc_start_kwh": 0.0, "soc_end_kwh": 0.2},
  {"base_load_kw": 0.2, "pv_kw": 0.0,
   "charge_ac_energy_kwh": 0.0, "discharge_ac_energy_kwh": 0.09,
   "grid_purchase_energy_kwh": 0.01, "soc_start_kwh": 0.2, "soc_end_kwh": 0.1}
]}
JSON
curl -s -X POST $BASE/dispatch/sessions/$SID/batches -H 'Content-Type: application/json' \
  -d @/tmp/batch0.json | jq '{result, confirmed_periods, next_index, status, first_record_soc_gap_kwh}'

# 3) 网络超时后原样重试同一起点同一批（起点、长度、内容都与已确认整批一致）
#    → result=deduped，不重复入库，回执与首收时完全相同
curl -s -X POST $BASE/dispatch/sessions/$SID/batches -H 'Content-Type: application/json' \
  -d @/tmp/batch0.json | jq '{result, confirmed_periods, next_index, status, final_soc_kwh}'

#    同起点短批（只重发第一条）不是已确认整批 → HTTP 409 conflict，记录与进度不变
jq -c '{start_index: 0, records: [.records[0]]}' /tmp/batch0.json \
  | curl -s -o /tmp/short.json -w '%{http_code}\n' \
    -X POST $BASE/dispatch/sessions/$SID/batches -H 'Content-Type: application/json' -d @-
jq .error /tmp/short.json   # "conflict"

#    同起点但内容不同（哪怕只改一个读数）→ HTTP 409 conflict
sed 's/"grid_purchase_energy_kwh": 0.01/"grid_purchase_energy_kwh": 0.012/' /tmp/batch0.json \
  | curl -s -o /tmp/conflict.json -w '%{http_code}\n' \
    -X POST $BASE/dispatch/sessions/$SID/batches -H 'Content-Type: application/json' -d @-
jq .error /tmp/conflict.json   # "conflict"

# 4) 第二批追加 t2、t3（start_index 必须接上批的 next_index=2，首态接已保存末态 0.1）
cat > /tmp/batch2.json <<'JSON'
{"start_index": 2, "records": [
  {"base_load_kw": 0.2, "pv_kw": 0.0,
   "charge_ac_energy_kwh": 0.0, "discharge_ac_energy_kwh": 0.09,
   "grid_purchase_energy_kwh": 0.01, "soc_start_kwh": 0.1, "soc_end_kwh": 0.0},
  {"base_load_kw": 0.2, "pv_kw": 0.0,
   "charge_ac_energy_kwh": 0.0, "discharge_ac_energy_kwh": 0.0,
   "grid_purchase_energy_kwh": 0.1, "soc_start_kwh": 0.0, "soc_end_kwh": 0.0}
]}
JSON
curl -s -X POST $BASE/dispatch/sessions/$SID/batches -H 'Content-Type: application/json' \
  -d @/tmp/batch2.json | jq '{result, confirmed_periods, next_index, status}'
# 若下标跳跃（如 start_index=3）或记录异常，会返回 422 且整批不入库，可修正后按 next_index 重发

# 4b) 后续批次入库后再重发完整首批：仍返回该批首次成功时的回执
#     （confirmed_periods=2、next_index=2、status=in_progress、final_soc_kwh=0.1），
#     不漂移到最新进度；会话实际进度以查询接口为准
curl -s -X POST $BASE/dispatch/sessions/$SID/batches -H 'Content-Type: application/json' \
  -d @/tmp/batch0.json | jq '{result, confirmed_periods, next_index, status, final_soc_kwh}'

#    跨批拼接（把两批合成一批重发）不等于任何已确认整批 → HTTP 409 conflict
jq -s '{start_index: 0, records: (.[0].records + .[1].records)}' /tmp/batch0.json /tmp/batch2.json \
  | curl -s -o /tmp/concat.json -w '%{http_code}\n' \
    -X POST $BASE/dispatch/sessions/$SID/batches -H 'Content-Type: application/json' -d @-
jq .error /tmp/concat.json   # "conflict"

# 5) 查询会话：原计划 + 连续已确认记录 + 下一下标（重启后同样可查）
curl -s $BASE/dispatch/sessions/$SID | tee /tmp/detail.json | jq \
  '{status, confirmed_periods, next_index, initial_soc_kwh, final_soc_kwh}'
curl -s $BASE/dispatch/sessions | jq '.sessions | length'

# 6) 满期（status=complete）后，直接用已确认记录结算
jq -c '{dispatch: .dispatch, plan_snapshot: .plan_snapshot,
        settlement_tolerance_kwh: 0.01,
        records: [.confirmed_records[].record]}' /tmp/detail.json \
  | curl -s -X POST $BASE/dispatch/settlement -H 'Content-Type: application/json' -d @- \
  | jq '{status, settled_periods, actual_total_cost: .summary.actual_total_cost_yuan}'

# 7) 若只确认了部分前缀（下例取已确认的前 2 段），可用连续前缀续排余段
jq -c '{dispatch, plan_snapshot, audit_tolerance_kwh,
            executed_records: [.confirmed_records[0:2][].record],
            remaining_prices: [5.0, 0.0],
            remaining_base_load_kw: [0.2, 0.2],
            remaining_pv_kw: [0.0, 0.0]}' /tmp/detail.json \
  | curl -s -X POST $BASE/dispatch/replan -H 'Content-Type: application/json' -d @- \
  | jq '{executed_periods, remaining_periods, total_cost_yuan}'
```

进程重启验证：`docker compose restart`（或 `docker compose down && docker compose up -d`，命名卷保留）后再次执行第 5 步，会话与已确认记录仍在；对已保存批次执行第 3、4b 步仍返回 `deduped`，且回执保持各批首次成功时的值。

## 模型与假设

- **时段**：固定 0.5 h；功率（kW）× 0.5 即该时段能量（kWh）。
- **动作与 SOC 网格**：动作即一个时段内电芯能量变化，离散为 0.1 kWh 的整数倍：正数充电、负数放电、0 静置；SOC 全程位于 `[0, capacity]` 的 0.1 kWh 网格上。容量、初始 SOC、最低末端 SOC 必须精确落在该网格上，否则报参数错误（不做静默取整）。
- **功率平衡**（每时段）：

  ```
  购电量 = max(0, 基础负荷 − 光伏 + 充电交流电量 − 放电交流电量)
  ```

  光伏优先供负荷，剩余可给电池充电；**不允许向电网倒送电**，无法消纳的光伏盈余计为弃光（`pv_curtailed_energy_kwh`），放电量因此不会超过同时段净负荷。
- **效率**：充电交流电量 = 电芯电量 / ηc；放电交流电量 = 电芯电量 × ηd。功率上限按交流侧考核（充电：电芯电量 ≤ P充·Δt·ηc；放电：电芯电量 ≤ P放·Δt/ηd）。
- **费用**：
  - 能量费 = 电价 × 购电量；
  - 衰减费 = `degradation_cost_per_kwh` × 电芯侧吞吐量，吞吐量 = Σ|充入电芯能量| + Σ|电芯放出能量|（充、放各计一次）；
  - 总成本 = 能量费 + 衰减费。
- **末端约束**：最后一个时段结束时 SOC ≥ `min_terminal_soc_kwh`。

### 动态规划

状态 `(t, j)`：第 `t` 个时段结束、SOC 处于第 `j` 格（j × 0.1 kWh）时的最优前缀。每格最多转移到「静置 / 充电 1..k 格 / 放电 1..k 格」，转移范围同时受功率上限、SOC 上下界和"禁止倒送"约束，转移成本即时算出能量费与衰减费。时间复杂度 O(N · C · P)，其中 C = 容量/0.1，P = 单时段最多动作格数。

### 破平局（保证结果确定、可复现）

当不同计划在数值容差内无法区分时，依次按：

1. **总成本**低者优先（能量费 + 衰减费）；
2. 总成本相同 → **峰值购电量**（各时段购电量的最大值，kWh/半小时）小者优先，即偏好削峰；
3. 仍相同 → **动作序列字典序**小者优先，动作为整数（0.1 kWh 格数，正充负放），越靠前的时段差异权重越大——因此"先静置、后动作"会胜过"先动作、后静置"。

## 错误处理

非法输入或无解统一返回 **HTTP 422**，JSON 说明原因：

```json
{"error": "invalid_input", "reason": "电价、基础负荷、光伏预测必须等长：当前分别为 3、2、3"}
```

```json
{"error": "infeasible", "reason": "无满足末端 SOC 约束的计划：调度期末最高可达 SOC 约为 0.0kWh，低于最低末端 SOC 0.1kWh；原因是受时段数、最大充电功率或充电效率限制，无法在期末充到要求电量"}
```

`invalid_input` 覆盖：数组不等长、负值/非有限值、效率不在 (0,1]、容量/SOC 非 0.1 整数倍、初始或末端 SOC 超过容量、数组长度超出 1–200 等；`infeasible` 表示输入合法但约束可行域为空，并给出最高可达末端 SOC 等诊断信息。`/telemetry/audit` 的非法输入（容量非正、效率越界、测量值或容差为负/非有限、SOC 超容量、记录数超出 1–2000 等）同样以 `invalid_input` 返回。

`/dispatch/settlement` 除沿用上述非法输入拦截（记录数与计划时段数不符等，`invalid_input`）外，另有两类整体拒绝：

```json
{"error": "plan_snapshot_mismatch", "reason": "计划快照第 0 个时段的 grid_purchase_energy_kwh=0.99 与重算结果 0.322222 不一致，快照可能已被篡改"}
```

```json
{"error": "no_valid_interval", "reason": "首条实测记录（下标 0）即异常或 SOC 断点，不存在可结算的连续有效区间：电芯能量残差 -0.150000kWh 超容差 0.01kWh（实测末态 SOC 0.05kWh，核算值 0.2kWh）"}
```

- `plan_snapshot_mismatch`：计划快照与按 `dispatch` 重算的结果在任一字段上不一致（含动作、逐时段能量/SOC/费用、summary），或快照结构缺失；
- `no_valid_interval`：首条记录即未通过遥测审计或 SOC 轨迹核对，没有任何可结算前缀；
- 部分记录异常 / 断点**不是**错误：返回 HTTP 200 与 `status=partially_settled`，仅冻结异常点及后续记录。

`/dispatch/replan` 同样为整体拒绝返回 422，错误码包括：

- `plan_snapshot_mismatch`：原计划快照与按 `dispatch` 重算的结果不一致（原输入本身无解为 `infeasible`）；
- `prefix_audit_failed`：首条实测首态 SOC 未在容差内接原初值，或连续前缀中存在遥测审计异常（电芯能量 / 购电量 / SOC 衔接）；
- `prefix_soc_off_grid`：前缀末态 SOC 不落在 0.1kWh 网格上（离散调度只能从整格 SOC 起步）；
- `infeasible`：从实测末态出发，原末端 SOC 约束在余段新预测下已无法满足；
- `invalid_input`：前缀为空或覆盖全周期、余段预测长度不等于剩余时段数、容差为负 / 非有限、测量值为负 / 非有限、SOC 超容量等。

执行会话接口的错误码（除 `conflict` 为 **HTTP 409**、`session_not_found` 为 **HTTP 404** 外，其余为 422）：

- `plan_snapshot_mismatch` / `infeasible`：创建会话时快照核对不一致 / 原输入重算无解，均不创建会话；
- `batch_index_gap`：`start_index` 大于会话 `next_index`（下标跳跃），整批不入库；
- `batch_out_of_range`：本批下标区间超出 `[0, num_periods)`，整批不入库；
- `batch_boundary_gap`：首批首态未在容差内接原初始 SOC，或后续批首态未接已保存末态，整批不入库；
- `batch_audit_failed`：批内任一条记录未通过遥测审计（原因点名首个异常下标），整批不入库；
- `conflict`（409）：重叠上报未与某个已确认整批完全一致——同起点短批 / 长批、内容变更、跨批拼接、批中起点、并发上报，或旧库只有逐段记录而无法确认历史批次边界；已保存记录与进度不变，请按原因中的 `next_index` 续报；
- `session_not_found`（404）：会话 ID 不存在；
- `invalid_input`：容差为负 / 非有限、批为空或超长、测量值为负 / 非有限、SOC 超容量等。

计量表更正接口的错误码（`POST/GET .../revisions`；除 `revision_conflict` 为 **HTTP 409**、`session_not_found` 为 **HTTP 404** 外，其余为 422）：

- `revision_not_complete`：会话未满期（未确认完全部时段），不允许更正/查询修订；
- `revision_invalid_index`：替换下标越界（合法范围 `[0, num_periods)`），原因点名下标；
- `revision_duplicate_index`：同一次提交内同一下标重复出现，整次拒绝；
- `revision_anchor_gap`：修订台账下标 0 首态 SOC 未在容差内接原初始 SOC；
- `revision_audit_failed`：整段重核中某时段电芯能量 / 购电量失衡或相邻记录 SOC 断点（原因点名首个异常下标与残差），整次拒绝、已有修订不变；
- `revision_conflict`（409）：期望修订号过期（完成会话从 0 起），错误体顶层附 `current_revision`，本次不写入；
- `session_not_found`（404）：会话 ID 不存在；
- `invalid_input`：期望修订号为负、`changes` 为空 / 超长、替换记录负值 / 非有限 / SOC 超容量等。

逐版结算凭证接口的错误码（`POST/GET .../vouchers`；除 `voucher_revision_conflict` 为 **HTTP 409**、`session_not_found` 为 **HTTP 404** 外，其余为 422）：

- `voucher_not_complete`：会话未满期，不能出单 / 查询凭证；
- `voucher_partial_settled`：该版台账只能结算首条异常 / SOC 断点之前的有效前缀（错误体附 `first_anomaly_index`、`settled_periods`），整次拒绝、不留下凭证；
- `voucher_revision_conflict`（409）：请求修订号**未出单且不是当前修订号**——漏开的过期中间修订（不能补开）或超前修订，错误体顶层附 `current_revision`，本次不写入；已出单的修订号即使过期也不走冲突，而是幂等返回原凭证；
- `no_valid_interval` / `plan_snapshot_mismatch` 等：沿用 `/dispatch/settlement` 的整体拒绝错误码（如首条即异常或 SOC 断点），同样不留下凭证；
- `session_not_found`（404）：会话 ID 不存在；
- `invalid_input`：期望修订号为负等请求体非法。

注意：已执行动作**偏离旧计划**本身不构成拒绝——只要实测记录自洽（含相邻记录 SOC 衔接），偏离只作为 `soc_gap_*` 与各费用差额信息返回，续排一律以实测末态为准。

## 测试

```bash
pip install pytest httpx
pytest tests/ -q
```

覆盖谷充峰放套利、效率损耗与衰减费核算、末端 SOC 约束、禁止倒送、光伏免费充电、无解诊断、各类非法输入，以及峰值与字典序两级破平局；遥测审计部分覆盖自洽记录通过、三类残差（电芯能量 / 购电量 / SOC 衔接）越限标记、容差边界（等于容差不判异常）、光伏盈余购电截断，以及容量 / 效率 / 容差 / 测量值的非法输入拦截。结算部分覆盖：全部有效全额结算（含容差内购电偏差计入费用差额、实测吞吐量衰减费核算、预测偏差场景）、审计异常与 SOC 断点起的前缀冻结、断点与衔接异常的区分、计划快照（periods / summary / 动作）篡改拒绝、dispatch 输入与快照不匹配拒绝、长度不符与无有效区间拒绝、重算无解传播，以及既有三个接口行为不变。续排部分覆盖：原时间轴已执行/待执行明细与费用口径、余段电价变化后的最优重排、实测偏离旧计划但自洽时保留原记录并从末态重排、容差内表计偏差照实计费、余段输入与快照的结算接口端到端衔接（含余段仅一段）、首态 SOC 容差边界（等于容差通过、超出拒绝），以及前缀审计异常、首态未接原初值、末态非 0.1kWh 网格、余段末端约束不可达（`infeasible`）、快照篡改、长度不符、空前缀/全周期前缀、非法容差/测量值/余段预测等各类整体拒绝。执行会话部分覆盖：创建时快照重算核对（含篡改、原输入无解）、分批追加的首批/后续批首态衔接（含容差边界）、动作偏离计划但自洽仍接受、下标跳跃与越界整批不入库且无副作用、批内核电芯能量/购电量/SOC 衔接异常整批拒绝、同起点原样重试返回 deduped、同起点内容不同（含部分重叠）返回 409、并发同起点上报只有一份生效（多线程）、查询返回原计划与连续已确认记录及下一下标、未完成前缀端到端续排、满期记录端到端结算、按同一路径重建存储模拟进程重启后记录仍在且可续报、空批/负值/SOC 超容量等非法输入拦截，以及既有四个接口行为不变。计量表更正部分覆盖：修订号 0 查询即原始台账且结算请求可直接用、首次更正在当前版本上整段重核并原子落盘（乱序提交按下标落位、未指定时段保持原值）、后续修订叠加与改回原值后差异下标清空、期望修订号过期返回 409 及当前号、下标重复/越界整次拒绝并点名下标、电芯能量/购电量失衡与相邻 SOC 断点（含只改中段不修衔接链）整次拒绝且已有修订不变、首态对原初始 SOC 的锚点核对与容差边界、替换记录负值/SOC 超容量/空变更等非法输入、未满期拒绝更正与查询、未知会话 404、更正不改写最初上报证据且批次重试仍 deduped 并保持冻结回执、修订台账端到端提交现有结算接口（修订口径计费、原始口径不受影响）、按同一路径重建存储模拟重启后修订仍可查询并在当前号上继续更正（旧号冲突）、并发同期望修订号只有一份生效（多线程），以及会话详情暴露当前修订号且列表口径不变。逐版结算凭证部分覆盖：修订号 0 首张凭证对最初上报记录全额结算且冻结的结算报告与现有结算接口逐字段一致、修订后应补/应退/无差额三档计费（含 t1 购电调整的费用传导与谷时改读数不影响费用）、差额相对最近一张已出凭证从而可跳过未出单的中间修订、同一修订号重试原样返回冻结凭证（result=existing，voucher_id/出单时间/计费栏/台账不变）且即使后续修订继续推进也不受影响、未出单的过期中间修订与超前修订均返回 409 voucher_revision_conflict 及当前号且不写入、台账只能结算有效前缀（voucher_partial_settled，附首异常下标与已结算段数）与首条即断点（no_valid_interval）均整次拒绝且凭证表为空、修复台账后可对新修订号正常出单、未满期出单/查询 422、未知会话 404、负修订号 422、按出单次序列表与当前修订待出单状态（voucher_revisions/next_revision_settled）、重启后凭证次序与冻结内容仍在且重试仍幂等并可继续出单（序号接续）、旧库自动补建凭证表、同修订多线程并发出单恰好一份落盘其余 existing、出单与更正多轮并发交错下凭证永远冻结最初台账或出单 409 而无部分数据，以及原会话、批次重试、修订查询与现有结算接口行为不变。
