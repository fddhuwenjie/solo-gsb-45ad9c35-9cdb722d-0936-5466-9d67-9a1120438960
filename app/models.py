"""请求 / 响应模型与输入合法性校验。"""

from __future__ import annotations

import math
from typing import Annotated, List, Literal, Optional, Sequence, Union

from pydantic import BaseModel, Field, model_validator

MAX_PERIODS = 200
MAX_AUDIT_RECORDS = 2000
MAX_BATCH_RECORDS = 200
MIN_EFFICIENCY_EXCLUSIVE = 0.0
MAX_EFFICIENCY_INCLUSIVE = 1.0

# 价格 / 功率为 0.5h 数据：基础负荷、光伏预测单位 kW，电价单位 元/kWh。
Prices = Annotated[List[float], Field(min_length=1, max_length=MAX_PERIODS)]
Loads = Annotated[List[float], Field(min_length=1, max_length=MAX_PERIODS)]
Pvs = Annotated[List[float], Field(min_length=1, max_length=MAX_PERIODS)]

NonNeg = Field(ge=0.0)
Positive = Field(gt=0.0)


def _validate_telemetry_records(
    records: Sequence["TelemetryRecord"], capacity_kwh: float
) -> None:
    """逐条校验实测记录：有限、非负、SOC 不超容量。审计与结算共用。"""
    for i, rec in enumerate(records):
        for name, label in (
            ("base_load_kw", "基础负荷"),
            ("pv_kw", "光伏功率"),
            ("charge_ac_energy_kwh", "交流充电量"),
            ("discharge_ac_energy_kwh", "交流放电量"),
            ("grid_purchase_energy_kwh", "电网购电量"),
            ("soc_start_kwh", "首态 SOC"),
            ("soc_end_kwh", "末态 SOC"),
        ):
            v = getattr(rec, name)
            if not math.isfinite(v):
                raise ValueError(f"第 {i} 条记录的{label}不是有限数值：{v}")
            if v < 0:
                raise ValueError(f"第 {i} 条记录的{label}不允许为负值：{v}")
        if rec.soc_start_kwh > capacity_kwh:
            raise ValueError(
                f"第 {i} 条记录的首态 SOC {rec.soc_start_kwh}kWh "
                f"超过电池容量 {capacity_kwh}kWh"
            )
        if rec.soc_end_kwh > capacity_kwh:
            raise ValueError(
                f"第 {i} 条记录的末态 SOC {rec.soc_end_kwh}kWh "
                f"超过电池容量 {capacity_kwh}kWh"
            )


class DispatchRequest(BaseModel):
    prices: Prices = Field(description="各时段电价（元/kWh），长度需与负荷、光伏一致")
    base_load_kw: Loads = Field(description="各时段基础负荷（kW）")
    pv_kw: Pvs = Field(description="各时段光伏预测功率（kW）")

    capacity_kwh: float = Field(gt=0.0, le=100.0, description="电池容量（kWh），须为 0.1 的整数倍")
    max_charge_kw: float = Field(ge=0.0, le=100.0, description="最大充电功率（kW，交流侧）")
    max_discharge_kw: float = Field(ge=0.0, le=100.0, description="最大放电功率（kW，交流侧）")

    charge_efficiency: float = Field(gt=0.0, le=1.0, description="充电效率（0, 1]")
    discharge_efficiency: float = Field(gt=0.0, le=1.0, description="放电效率（0, 1]")

    initial_soc_kwh: float = Field(ge=0.0, description="初始 SOC（kWh），须为 0.1 的整数倍")
    min_terminal_soc_kwh: float = Field(ge=0.0, description="最低末端 SOC（kWh），须为 0.1 的整数倍")
    degradation_cost_per_kwh: float = Field(ge=0.0, description="电芯每吞吐 1kWh 的循环衰减成本（元/kWh）")

    @model_validator(mode="after")
    def _validate(self) -> "DispatchRequest":
        # 1. 等长
        n = len(self.prices)
        if len(self.base_load_kw) != n or len(self.pv_kw) != n:
            raise ValueError(
                f"电价、基础负荷、光伏预测必须等长：当前分别为 {n}、"
                f"{len(self.base_load_kw)}、{len(self.pv_kw)}"
            )

        # 2. 有限且非负（数组字段）
        for name, seq in (
            ("电价", self.prices),
            ("基础负荷", self.base_load_kw),
            ("光伏预测", self.pv_kw),
        ):
            for i, v in enumerate(seq):
                if not math.isfinite(v):
                    raise ValueError(f"{name}第 {i} 个时段不是有限数值：{v}")
                if v < 0:
                    raise ValueError(f"{name}不允许为负值，第 {i} 个时段为 {v}")

        # 3. 标量有限性
        for name in (
            "capacity_kwh",
            "max_charge_kw",
            "max_discharge_kw",
            "charge_efficiency",
            "discharge_efficiency",
            "initial_soc_kwh",
            "min_terminal_soc_kwh",
            "degradation_cost_per_kwh",
        ):
            v = getattr(self, name)
            if not math.isfinite(v):
                raise ValueError(f"{name} 必须是有限数值")

        # 4. 0.1kWh 网格
        from app.solver import SOC_STEP_KWH, _snap_steps

        for name, v in (
            ("电池容量", self.capacity_kwh),
            ("初始 SOC", self.initial_soc_kwh),
            ("最低末端 SOC", self.min_terminal_soc_kwh),
        ):
            _snap_steps(v, name)  # 非整数倍会抛中文 ValueError

        cap_steps = round(self.capacity_kwh / SOC_STEP_KWH)
        init_steps = round(self.initial_soc_kwh / SOC_STEP_KWH)
        end_steps = round(self.min_terminal_soc_kwh / SOC_STEP_KWH)
        if init_steps > cap_steps:
            raise ValueError("初始 SOC 不能超过电池容量")
        if end_steps > cap_steps:
            raise ValueError("最低末端 SOC 不能超过电池容量")

        return self


class PeriodPlan(BaseModel):
    index: int
    action: Literal["charge", "discharge", "idle"]
    action_energy_kwh: float
    charge_ac_energy_kwh: float
    discharge_ac_energy_kwh: float
    battery_power_kw: float
    grid_purchase_energy_kwh: float
    pv_curtailed_energy_kwh: float
    soc_start_kwh: float
    soc_end_kwh: float
    energy_cost_yuan: float
    degradation_cost_yuan: float


class DispatchSummary(BaseModel):
    num_periods: int
    period_hours: float
    soc_resolution_kwh: float
    total_energy_cost_yuan: float
    total_degradation_cost_yuan: float
    total_cost_yuan: float
    total_grid_purchase_kwh: float
    peak_grid_purchase_energy_kwh: float
    peak_grid_purchase_power_kw: float
    battery_throughput_kwh: float
    terminal_soc_kwh: float


class DispatchResponse(BaseModel):
    periods: List[PeriodPlan]
    summary: DispatchSummary


class TelemetryRecord(BaseModel):
    """一条半小时实测记录：功率单位 kW，电量与 SOC 单位 kWh。"""

    base_load_kw: float = Field(description="基础负荷功率（kW）")
    pv_kw: float = Field(description="光伏功率（kW）")
    charge_ac_energy_kwh: float = Field(description="交流侧充电量（kWh）")
    discharge_ac_energy_kwh: float = Field(description="交流侧放电量（kWh）")
    grid_purchase_energy_kwh: float = Field(description="电网购电量（kWh）")
    soc_start_kwh: float = Field(description="时段首 SOC（kWh）")
    soc_end_kwh: float = Field(description="时段末 SOC（kWh）")


class TelemetryAuditRequest(BaseModel):
    capacity_kwh: float = Field(gt=0.0, description="电池容量（kWh），须为正")
    charge_efficiency: float = Field(gt=0.0, le=1.0, description="充电效率（0, 1]")
    discharge_efficiency: float = Field(gt=0.0, le=1.0, description="放电效率（0, 1]")
    energy_tolerance_kwh: float = Field(ge=0.0, description="能量容差（kWh），非负")
    records: Annotated[
        List[TelemetryRecord], Field(min_length=1, max_length=MAX_AUDIT_RECORDS)
    ] = Field(description="按时间顺序排列的半小时实测记录")

    @model_validator(mode="after")
    def _validate(self) -> "TelemetryAuditRequest":
        # 1. 标量有限性（gt/ge 比较不拦截 inf）
        for name in (
            "capacity_kwh",
            "charge_efficiency",
            "discharge_efficiency",
            "energy_tolerance_kwh",
        ):
            v = getattr(self, name)
            if not math.isfinite(v):
                raise ValueError(f"{name} 必须是有限数值")

        # 2. 逐条记录：有限、非负、SOC 不超容量
        _validate_telemetry_records(self.records, self.capacity_kwh)

        return self


class TelemetryRecordAudit(BaseModel):
    index: int
    expected_soc_end_kwh: float = Field(description="按首态 SOC 与交流充放电量核算的期望末态 SOC")
    soc_residual_kwh: float = Field(description="实测末态 SOC − 期望末态 SOC")
    expected_grid_purchase_energy_kwh: float = Field(description="按功率平衡核算的期望购电量")
    grid_residual_kwh: float = Field(description="实测购电量 − 期望购电量")
    soc_continuity_gap_kwh: float = Field(description="首态 SOC − 上一条末态 SOC（首条为 0）")
    anomalous: bool = Field(description="任一分项残差绝对值超容差即为 true")
    reasons: List[str] = Field(description="触发异常的具体原因")


class TelemetryAuditResponse(BaseModel):
    num_records: int
    period_hours: float
    energy_tolerance_kwh: float
    anomalous: bool = Field(description="是否存在异常记录")
    anomalous_records: List[int] = Field(description="异常记录的下标")
    records: List[TelemetryRecordAudit]


class SettlementRequest(BaseModel):
    """计划执行结算请求：一次 /dispatch 的完整输入、其计划快照、容差与等长实测记录。"""

    dispatch: DispatchRequest = Field(description="生成该计划时使用的完整 /dispatch 输入")
    plan_snapshot: DispatchResponse = Field(
        description="当初 /dispatch 返回的计划快照（periods + summary），服务端会重算并核对"
    )
    settlement_tolerance_kwh: float = Field(
        ge=0.0, description="结算容差（kWh），同时用于遥测残差与 SOC 轨迹偏离判定"
    )
    records: Annotated[
        List[TelemetryRecord], Field(min_length=1, max_length=MAX_PERIODS)
    ] = Field(description="按时间顺序排列的半小时实测记录，长度须与计划时段数一致")

    @model_validator(mode="after")
    def _validate(self) -> "SettlementRequest":
        if not math.isfinite(self.settlement_tolerance_kwh):
            raise ValueError("settlement_tolerance_kwh 必须是有限数值")
        # 记录的有限/非负/SOC 容量约束与审计接口一致（容量取 dispatch 输入口径）
        _validate_telemetry_records(self.records, self.dispatch.capacity_kwh)
        n = len(self.records)
        planned_n = len(self.plan_snapshot.periods)
        if n != planned_n:
            raise ValueError(
                f"实测记录长度 {n} 与计划快照时段数 {planned_n} 不符，无法逐时段结算"
            )
        return self


class SettlementPeriodPlan(BaseModel):
    """结算明细中回显的该时段计划口径数据。"""

    action: Literal["charge", "discharge", "idle"]
    grid_purchase_energy_kwh: float
    battery_throughput_kwh: float
    energy_cost_yuan: float
    degradation_cost_yuan: float
    soc_start_kwh: float
    soc_end_kwh: float


class SettlementRecordAudit(TelemetryRecordAudit):
    """遥测审计结论外，附加结算专用的 SOC 断点判定。"""

    soc_breakpoint: bool = Field(description="首/末 SOC 偏离计划轨迹超容差")
    soc_breakpoint_reasons: List[str] = Field(description="SOC 断点的具体原因")


class SettlementPeriodResult(BaseModel):
    index: int
    status: Literal["settled", "pending"] = Field(
        description="settled=已结算；pending=首条异常/SOC 断点起冻结的待结算记录"
    )
    plan: SettlementPeriodPlan
    soc_gap_start_kwh: float = Field(description="实测首态 SOC − 计划首态 SOC")
    soc_gap_end_kwh: float = Field(description="实测末态 SOC − 计划末态 SOC")

    actual_grid_purchase_energy_kwh: Optional[float] = Field(description="实测购电量（待结算为 null）")
    actual_battery_throughput_kwh: Optional[float] = Field(
        description="实测电芯侧吞吐量 = 交流充电×ηc + 交流放电÷ηd（待结算为 null）"
    )
    actual_energy_cost_yuan: Optional[float]
    actual_degradation_cost_yuan: Optional[float]
    actual_total_cost_yuan: Optional[float]

    planned_energy_cost_yuan: float
    planned_degradation_cost_yuan: float
    planned_total_cost_yuan: float

    energy_cost_delta_yuan: Optional[float] = Field(description="实测能量费 − 计划能量费")
    degradation_cost_delta_yuan: Optional[float] = Field(description="实测衰减费 − 计划衰减费")
    total_cost_delta_yuan: Optional[float] = Field(description="实测总费用 − 计划总费用")
    grid_purchase_delta_kwh: Optional[float] = Field(description="实测购电量 − 计划购电量")
    throughput_delta_kwh: Optional[float] = Field(description="实测吞吐量 − 计划吞吐量")

    audit: SettlementRecordAudit
    reasons: List[str] = Field(description="待结算/断点原因（已结算记录为空）")


class SettlementSummary(BaseModel):
    settled_periods: int
    actual_grid_purchase_kwh: float
    planned_grid_purchase_kwh: float
    grid_purchase_delta_kwh: float
    actual_battery_throughput_kwh: float
    planned_battery_throughput_kwh: float
    throughput_delta_kwh: float
    actual_energy_cost_yuan: float
    planned_energy_cost_yuan: float
    energy_cost_delta_yuan: float
    actual_degradation_cost_yuan: float
    planned_degradation_cost_yuan: float
    degradation_cost_delta_yuan: float
    actual_total_cost_yuan: float
    planned_total_cost_yuan: float
    total_cost_delta_yuan: float


class SettlementAuditSummary(BaseModel):
    anomalous: bool
    anomalous_records: List[int]


class SettlementResponse(BaseModel):
    num_records: int
    period_hours: float
    settlement_tolerance_kwh: float
    status: Literal["settled", "partially_settled"] = Field(
        description="settled=全部记录连续有效已结算；partially_settled=存在异常/SOC 断点，"
        "仅前缀区间已结算，其余待结算"
    )
    settled_periods: int = Field(description="已结算的连续前缀时段数（自下标 0 起）")
    pending_periods: int = Field(description="待结算时段数")
    first_anomaly_index: Optional[int] = Field(
        description="首条异常或 SOC 断点的下标；全部有效时为 null"
    )
    anomalous_records: List[int] = Field(description="遥测审计不通过的记录下标")
    soc_breakpoint_records: List[int] = Field(description="SOC 偏离计划轨迹的记录下标")
    periods: List[SettlementPeriodResult]
    summary: SettlementSummary
    audit_summary: SettlementAuditSummary


class ReplanRequest(BaseModel):
    """中途续排请求：原始 /dispatch 输入、其计划快照、连续实测前缀与余段新预测。"""

    dispatch: DispatchRequest = Field(description="生成原计划时使用的完整 /dispatch 输入")
    plan_snapshot: DispatchResponse = Field(
        description="当初 /dispatch 返回的计划快照（periods + summary），服务端会重算并核对"
    )
    audit_tolerance_kwh: float = Field(
        ge=0.0, description="审计容差（kWh），沿用遥测审计规则：残差绝对值严格大于它即判异常"
    )
    executed_records: Annotated[
        List[TelemetryRecord], Field(min_length=1, max_length=MAX_PERIODS - 1)
    ] = Field(description="自原时间轴起点连续已执行的半小时实测记录，至少一段、且不得覆盖全周期")
    remaining_prices: Prices = Field(description="剩余时段的新电价预测（元/kWh），至少一段")
    remaining_base_load_kw: Loads = Field(description="剩余时段的新基础负荷预测（kW），至少一段")
    remaining_pv_kw: Pvs = Field(description="剩余时段的新光伏预测功率（kW），至少一段")

    @model_validator(mode="after")
    def _validate(self) -> "ReplanRequest":
        if not math.isfinite(self.audit_tolerance_kwh):
            raise ValueError("audit_tolerance_kwh 必须是有限数值")
        # 实测记录的有限/非负/SOC 容量约束与审计接口一致（容量取 dispatch 输入口径）
        _validate_telemetry_records(self.executed_records, self.dispatch.capacity_kwh)
        n = len(self.plan_snapshot.periods)
        k = len(self.executed_records)
        if k >= n:
            raise ValueError(
                f"已执行实测记录 {k} 段须少于原计划时段数 {n}，续排需留有待执行时段"
            )
        remaining_n = n - k
        if (
            len(self.remaining_prices) != remaining_n
            or len(self.remaining_base_load_kw) != remaining_n
            or len(self.remaining_pv_kw) != remaining_n
        ):
            raise ValueError(
                f"余段预测长度应为 {remaining_n}（原计划 {n} 段 − 已执行 {k} 段），"
                f"当前电价、基础负荷、光伏分别为 "
                f"{len(self.remaining_prices)}、{len(self.remaining_base_load_kw)}、"
                f"{len(self.remaining_pv_kw)}"
            )
        for name, seq in (
            ("余段电价", self.remaining_prices),
            ("余段基础负荷", self.remaining_base_load_kw),
            ("余段光伏预测", self.remaining_pv_kw),
        ):
            for i, v in enumerate(seq):
                if not math.isfinite(v):
                    raise ValueError(f"{name}第 {i} 个时段不是有限数值：{v}")
                if v < 0:
                    raise ValueError(f"{name}不允许为负值，第 {i} 个时段为 {v}")
        return self


class ReplanExecutedPeriod(BaseModel):
    """原时间轴上已执行时段的明细：实测口径计费 + 原计划回显 + 审计残差。"""

    index: int = Field(description="原时间轴上的时段下标")
    status: Literal["executed"]
    record: TelemetryRecord = Field(description="原始实测记录，原样保留（含对旧计划的偏离）")
    plan: SettlementPeriodPlan = Field(description="原计划该时段的计划口径数据")
    soc_gap_start_kwh: float = Field(description="实测首态 SOC − 原计划首态 SOC")
    soc_gap_end_kwh: float = Field(description="实测末态 SOC − 原计划末态 SOC")

    actual_grid_purchase_energy_kwh: float
    actual_battery_throughput_kwh: float = Field(
        description="实测电芯侧吞吐量 = 交流充电×ηc + 交流放电÷ηd"
    )
    actual_energy_cost_yuan: float
    actual_degradation_cost_yuan: float
    actual_total_cost_yuan: float

    planned_energy_cost_yuan: float
    planned_degradation_cost_yuan: float
    planned_total_cost_yuan: float

    energy_cost_delta_yuan: float = Field(description="实测能量费 − 原计划能量费")
    degradation_cost_delta_yuan: float
    total_cost_delta_yuan: float
    grid_purchase_delta_kwh: float
    throughput_delta_kwh: float

    audit: TelemetryRecordAudit
    reasons: List[str] = Field(
        description="恒为空数组：能进入续排说明前缀审计全部通过；SOC 对旧计划的偏离不是异常"
    )


class ReplanPlannedPeriod(PeriodPlan):
    """余段新计划的逐时段计划（字段同 /dispatch 的 PeriodPlan），附加状态与时间轴下标。"""

    status: Literal["planned"]
    remaining_index: int = Field(description="在余段内的相对下标（从 0 起）")


ReplanTimelinePeriod = Annotated[
    Union[ReplanExecutedPeriod, ReplanPlannedPeriod],
    Field(discriminator="status"),
]


class ReplanExecutedSummary(BaseModel):
    """实测前缀汇总（按 /dispatch/settlement 现有结算口径）。"""

    executed_periods: int
    final_soc_kwh: float = Field(description="前缀末态 SOC（余段调度的初始 SOC，落在 0.1kWh 网格）")
    actual_grid_purchase_kwh: float
    planned_grid_purchase_kwh: float
    grid_purchase_delta_kwh: float
    actual_battery_throughput_kwh: float
    planned_battery_throughput_kwh: float
    throughput_delta_kwh: float
    actual_energy_cost_yuan: float
    planned_energy_cost_yuan: float
    energy_cost_delta_yuan: float
    actual_degradation_cost_yuan: float
    planned_degradation_cost_yuan: float
    degradation_cost_delta_yuan: float
    actual_total_cost_yuan: float
    planned_total_cost_yuan: float
    total_cost_delta_yuan: float


class ReplanResponse(BaseModel):
    num_periods: int = Field(description="原计划总时段数")
    period_hours: float
    audit_tolerance_kwh: float
    executed_periods: int = Field(description="已执行连续前缀时段数（自下标 0 起）")
    remaining_periods: int = Field(description="余段待执行时段数")

    periods: List[ReplanTimelinePeriod] = Field(
        description="原时间轴上已执行（executed）与待执行（planned）的逐时段明细，按 index 升序"
    )

    executed_summary: ReplanExecutedSummary
    remaining_summary: DispatchSummary = Field(
        description="余段新计划的汇总（口径同 /dispatch 的 summary；费用仅覆盖余段）"
    )

    actual_prefix_total_cost_yuan: float = Field(description="实测前缀总费用（能量费+衰减费）")
    remaining_forecast_total_cost_yuan: float = Field(description="余段预测总费用（能量费+衰减费）")
    total_cost_yuan: float = Field(description="前缀实测费用 + 余段预测费用")

    remaining_dispatch: DispatchRequest = Field(
        description="可直接 POST 给 /dispatch/settlement 的余段调度输入："
        "初始 SOC 取前缀末态，数组只覆盖余段新预测，末端 SOC 约束沿用原输入"
    )
    remaining_plan_snapshot: DispatchResponse = Field(
        description="可与 remaining_dispatch 一起交给 /dispatch/settlement 的余段计划快照"
    )


class ErrorResponse(BaseModel):
    error: str
    reason: str


# ---------------------------------------------------------------------------
# 可持久接续的执行会话
# ---------------------------------------------------------------------------


class SessionCreateRequest(BaseModel):
    """创建执行会话：原 /dispatch 输入、完整计划快照与审计容差。"""

    dispatch: DispatchRequest = Field(description="生成该计划时使用的完整 /dispatch 输入")
    plan_snapshot: DispatchResponse = Field(
        description="当初 /dispatch 返回的计划快照（periods + summary 原样提交），"
        "服务端会重算并逐字段核对一致"
    )
    audit_tolerance_kwh: float = Field(
        ge=0.0, description="审计容差（kWh），规则同 /telemetry/audit：残差绝对值严格大于它即判异常"
    )

    @model_validator(mode="after")
    def _validate(self) -> "SessionCreateRequest":
        if not math.isfinite(self.audit_tolerance_kwh):
            raise ValueError("audit_tolerance_kwh 必须是有限数值")
        return self


class ConfirmedRecord(BaseModel):
    """已确认记录：原始实测记录 + 全局时段下标 + 入库时间。"""

    index: int = Field(description="原计划时间轴上的时段下标（从 0 起）")
    record: TelemetryRecord
    stored_at: str = Field(description="该记录入库时间（UTC ISO 8601）")


class SessionCreateResponse(BaseModel):
    session_id: str = Field(description="会话 ID（UUID）")
    created_at: str = Field(description="会话创建时间（UTC ISO 8601）")
    audit_tolerance_kwh: float
    num_periods: int = Field(description="原计划时段总数，即可追加的下标范围 [0, num_periods)")
    confirmed_periods: int = Field(description="已确认时段数（创建时恒为 0）")
    next_index: int = Field(description="下一批应使用的起始下标（创建时恒为 0）")
    status: Literal["awaiting_records", "in_progress", "complete"]
    dispatch: DispatchRequest = Field(description="回显原 /dispatch 输入")
    plan_snapshot: DispatchResponse = Field(description="回显已核对一致的计划快照")


class BatchAppendRequest(BaseModel):
    """按起始下标成批追加半小时实测记录。"""

    start_index: int = Field(ge=0, description="本批首条记录在原时间轴上的下标，须等于会话 next_index")
    records: Annotated[
        List[TelemetryRecord], Field(min_length=1, max_length=MAX_BATCH_RECORDS)
    ] = Field(description="本批半小时实测记录（按时间顺序，下标连续递增）")
    # 记录的有限/非负/SOC 容量约束在服务层按会话所属计划的容量统一校验


class BatchAuditRecord(TelemetryRecordAudit):
    """批内逐条审计结论：index 为原时间轴全局下标。"""


class BatchAppendResponse(BaseModel):
    session_id: str
    start_index: int = Field(description="本批起始下标（与请求一致）")
    batch_size: int = Field(description="本批记录条数")
    result: Literal["accepted", "deduped"] = Field(
        description="accepted=新批次已入库；deduped=起点、长度、内容均与某已确认整批"
        "一致的原样重试，返回该批首次成功时冻结的回执"
    )
    confirmed_periods: int = Field(
        description="本批首次入库后会话的累计确认时段数（deduped 时保持首收时的值，"
        "不随后续批次入库而变化）"
    )
    next_index: int = Field(
        description="本批首次入库后的下一起始下标（deduped 时保持首收时的值）"
    )
    status: Literal["awaiting_records", "in_progress", "complete"]
    batch_audit: List[BatchAuditRecord] = Field(
        description="本批记录的遥测审计逐条结论（deduped 时为已保存批次首收时的同一审计结果）"
    )
    first_record_soc_gap_kwh: float = Field(
        description="本批首态 SOC 与应有起点之差："
        "首批接原初始 SOC，后续批接已保存末态（deduped 时为首收回执中的同一值）"
    )
    final_soc_kwh: Optional[float] = Field(
        description="截至本批首次入库时的已确认末态 SOC"
        "（deduped 时保持首收时的值，不随后续批次漂移）"
    )


class SessionSummary(BaseModel):
    session_id: str
    created_at: str
    updated_at: Optional[str] = Field(description="最近一次成功入库时间（UTC ISO 8601），无记录为 null")
    audit_tolerance_kwh: float
    num_periods: int
    confirmed_periods: int
    next_index: int
    status: Literal["awaiting_records", "in_progress", "complete"]
    initial_soc_kwh: float = Field(description="原初始 SOC（首批首态须在容差内与之衔接）")
    final_soc_kwh: Optional[float] = Field(description="已确认末态 SOC，无记录为 null")


class SessionListResponse(BaseModel):
    sessions: List[SessionSummary]


class SessionDetailResponse(BaseModel):
    """查询会话：原计划信息 + 连续已确认记录 + 下一下标。"""

    session_id: str
    created_at: str
    updated_at: Optional[str]
    audit_tolerance_kwh: float
    num_periods: int
    confirmed_periods: int
    next_index: int
    current_revision: int = Field(
        description="当前计量表修订号：未更正为 0，每次成功更正 +1；"
        "此处的 confirmed_records 永远是最初上报记录，不受修订影响"
    )
    status: Literal["awaiting_records", "in_progress", "complete"]
    dispatch: DispatchRequest = Field(description="原 /dispatch 输入，可直接用于续排/结算")
    plan_snapshot: DispatchResponse = Field(description="已核对一致的原计划快照")
    confirmed_records: List[ConfirmedRecord] = Field(
        description="自下标 0 起连续已确认的实测记录（未完成前缀可用于 /dispatch/replan；"
        "满期记录可用于 /dispatch/settlement）；始终为最初上报记录，更正不改写它们"
    )
    confirmed_audit: List[BatchAuditRecord] = Field(
        description="对全部连续已确认记录重算的遥测审计（全局下标）"
    )
    initial_soc_kwh: float
    final_soc_kwh: Optional[float]


# ---------------------------------------------------------------------------
# 计量表更正（修订台账）：保留最初上报证据，叠加提供修订后的实测台账
# ---------------------------------------------------------------------------


class RevisionChange(BaseModel):
    """一条时段级完整替换：指定下标的整条实测记录被替换（不是字段级 patch）。"""

    index: int = Field(ge=0, description="被替换时段在原时间轴上的下标（从 0 起）")
    record: TelemetryRecord = Field(
        description="该下标的完整替换记录（七个测量字段须全部给出，缺字段即按缺省值替换）"
    )


class RevisionCreateRequest(BaseModel):
    """计量表更正提交：期望修订号 + 至少一个「下标 + 完整替换记录」。"""

    expected_revision: int = Field(
        ge=0,
        description="客户端基于的当前修订号（乐观并发）：完成会话从 0 开始，"
        "与服务端当前号不符返回 409 冲突及当前号",
    )
    changes: Annotated[
        List[RevisionChange], Field(min_length=1, max_length=MAX_PERIODS)
    ] = Field(description="本次替换的时段记录（下标重复或越界整次拒绝）")


class RevisionChangeRecord(BaseModel):
    """修订台账中的一条：全局下标 + 该下标当前生效的完整实测记录。"""

    index: int = Field(description="原计划时间轴上的时段下标（从 0 起）")
    record: TelemetryRecord = Field(description="该下标当前生效的实测记录（可能来自某次更正替换）")


class OriginalRecord(BaseModel):
    """最初上报证据：下标 + 原始记录 + 入库时间，更正永不改写。"""

    index: int
    record: TelemetryRecord
    stored_at: str = Field(description="该记录最初入库时间（UTC ISO 8601）")


class RevisionHistoryItem(BaseModel):
    """一次成功更正的历史条目：新修订号、原始提交的变更与确认时间。"""

    revision_no: int = Field(description="该次更正落盘后的修订号（从 1 起）")
    changes: List[RevisionChange] = Field(description="该次提交的原始「下标 + 完整替换记录」")
    changed_indices: List[int] = Field(description="该次提交涉及的下标，按提交顺序去重升序")
    created_at: str = Field(description="该次更正确认时间（UTC ISO 8601）")


class SettlementRequestBody(BaseModel):
    """可直接 POST 给 /dispatch/settlement 的完整请求体（记录取当前修订台账）。"""

    dispatch: DispatchRequest
    plan_snapshot: DispatchResponse
    settlement_tolerance_kwh: float = Field(
        description="取会话创建时提交的审计容差 audit_tolerance_kwh"
    )
    records: List[TelemetryRecord] = Field(
        description="当前修订后的整段实测台账（未被替换的时段保持最初上报值）"
    )


class RevisionDetailResponse(BaseModel):
    """计量表更正/查询响应：原始证据、当前修订台账、差异下标与可提交的结算请求。"""

    session_id: str
    num_periods: int
    confirmed_periods: int
    status: Literal["complete"] = Field(description="仅满期（complete）会话允许更正")
    current_revision: int = Field(description="当前修订号：首次更正成功后为 1，之后叠加递增")
    audit_tolerance_kwh: float
    original_records: List[OriginalRecord] = Field(
        description="最初上报的实测台账（证据原样保留，任何更正都不改写）"
    )
    revised_records: List[RevisionChangeRecord] = Field(
        description="当前修订后的整段实测台账（未指定时段保持原值）"
    )
    changed_indices: List[int] = Field(
        description="当前修订台账相对最初上报有差异的下标（升序）"
    )
    revisions: List[RevisionHistoryItem] = Field(
        description="历次更正历史（修订号、原始变更、确认时间），未更正时为空"
    )
    revised_audit: List[BatchAuditRecord] = Field(
        description="用会话容量、效率、初始 SOC 与审计容差对整段修订台账重算的遥测审计"
    )
    initial_soc_kwh: float
    final_soc_kwh: float = Field(description="当前修订台账最后一条记录的末态 SOC")
    settlement_request: SettlementRequestBody = Field(
        description="可直接整体 POST 给 /dispatch/settlement 的完整请求数据"
    )


# ---------------------------------------------------------------------------
# 满期执行会话的逐版结算凭证
# ---------------------------------------------------------------------------


class VoucherIssueRequest(BaseModel):
    """出单请求：指定要结算的修订号（0 = 最初上报记录，其余为对应更正台账）。"""

    expected_revision: int = Field(
        ge=0,
        description="要求出单的修订号：0 对最初上报记录结算，n≥1 对修订号 n 的"
        "更正台账结算；该修订未出单且不是当前修订号时返回 409 冲突及当前号",
    )


class VoucherBilling(BaseModel):
    """凭证计费栏：首张记全额，其后为相对上一张凭证的应补/应退差额。"""

    type: Literal["full", "supplement", "refund", "no_adjustment"] = Field(
        description="full=本会话首张凭证，按实测总费用全额；supplement=相对上一张"
        "应补（本版总费用更高）；refund=相对上一张应退（本版总费用更低）；"
        "no_adjustment=两张凭证实测总费用相等，差额为 0"
    )
    amount_yuan: float = Field(
        description="full 时为实测总费用全额；补退时为差额绝对值；no_adjustment 时为 0"
    )
    relative_to_revision: Optional[int] = Field(
        description="差额对比的上一张凭证的修订号；首张为 null"
    )
    relative_to_voucher_no: Optional[int] = Field(
        description="差额对比的上一张凭证的出单序号；首张为 null"
    )


class SettlementVoucherResponse(BaseModel):
    """一张可追溯的逐版结算凭证（落盘后内容冻结，重试同一修订号原样返回）。"""

    voucher_id: str = Field(description="凭证 ID（UUID），落盘后不变")
    session_id: str
    voucher_no: int = Field(description="本会话内的出单序号（从 1 起，按出单次序）")
    result: Literal["issued", "existing"] = Field(
        description="issued=本次新出单并原子落盘；existing=该修订号已有凭证，"
        "原样返回冻结内容（即使后来又有更新的修订）"
    )
    revision_no: int = Field(description="凭证所结算的修订号（0 = 最初上报记录）")
    current_revision: int = Field(
        description="出单当时（或已冻结凭证落盘时）的会话当前修订号"
    )
    total_cost_yuan: float = Field(
        description="该版台账按现有结算规则核算的实测总费用（能量费+衰减费）"
    )
    settlement_status: Literal["settled", "partially_settled"] = Field(
        description="沿用 /dispatch/settlement 的核算结论；凭证只接受全额可结算"
    )
    settled_periods: int = Field(description="已结算时段数（凭证落盘时必等于计划时段数）")
    num_periods: int
    billing: VoucherBilling = Field(
        description="计费栏：首张全额，其后列出相对上一张凭证的应补/应退差额"
        "（允许跳过未出单的中间修订）"
    )
    issued_at: str = Field(description="出单时间（UTC ISO 8601，落盘后不变）")
    records: List[RevisionChangeRecord] = Field(
        description="出单时冻结的该修订版完整实测台账（后续更正不影响已出凭证）"
    )
    settlement: SettlementResponse = Field(
        description="出单时冻结的完整结算报告（逐时段明细、计划/实测/差额汇总等，"
        "口径与 POST /dispatch/settlement 完全一致）"
    )


class VoucherPendingStatus(BaseModel):
    """当前修订的待出单状态。"""

    current_revision: int = Field(description="会话当前修订号（未更正为 0）")
    voucher_revisions: List[int] = Field(
        description="已出单的修订号列表（按出单次序，未必连续）"
    )
    next_revision_settled: bool = Field(
        description="当前修订号是否已有凭证（true=当前修订已出单，无待出单修订；"
        "false=当前修订尚未出单）"
    )


class VoucherListResponse(BaseModel):
    """按出单次序查询凭证 + 当前修订待出单状态。"""

    session_id: str
    num_periods: int
    status: Literal["complete"] = Field(description="仅满期（complete）会话可出单/查询")
    current_revision: int = Field(description="会话当前修订号（未更正为 0）")
    pending: VoucherPendingStatus = Field(description="当前修订待出单状态")
    vouchers: List[SettlementVoucherResponse] = Field(
        description="全部已出凭证，按出单次序（voucher_no 升序）；每张内容为冻结值"
    )
