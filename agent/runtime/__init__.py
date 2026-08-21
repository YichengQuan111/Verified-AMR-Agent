"""Runtime state, P0-14 recovery contracts and P0-13 PEVR graph exports.

PEVR 图类型采用延迟导出：``services.amr_simulator.contracts`` 需要从本包读取
``Observation``，若这里启动时立即导入 PEVR 的 SimulationPlan 会形成循环导入。
因此 P0-04 状态与 P0-14 纯契约先直接导出，P0-13 图和报告在真正使用时再加载。
"""

from agent.runtime.state import (
    ConstraintViolation,
    FaultRecord,
    Observation,
    ObservationSource,
    ObservationStatus,
    RunState,
    RunStatus,
)
from agent.runtime.checkpoint import (
    CheckpointSnapshot,
    EffectLedgerEntry,
    EffectLedgerStatus,
    EffectReservation,
    ExternalExecutionSnapshot,
    ExternalExecutionStatus,
    InMemoryExternalStateReconciler,
    InMemoryRuntimeStore,
    RecoveryAssessment,
    RecoveryCoordinator,
    RecoveryDecision,
    make_effect_idempotency_key,
)
from agent.runtime.hitl import (
    ApprovalGrant,
    HITLInterrupt,
    HITLInterruptError,
    HITLController,
    HITLReason,
    HITLRequest,
    HITLStatus,
    InMemoryHITLStore,
)
from agent.runtime.trace import TraceCollector, TraceError, TraceEvent, new_trace_id

__all__ = [
    "ConstraintViolation",
    "FaultRecord",
    "Observation",
    "ObservationSource",
    "ObservationStatus",
    "RunState",
    "RunStatus",
    "CheckpointSnapshot",
    "EffectLedgerEntry",
    "EffectLedgerStatus",
    "EffectReservation",
    "ExternalExecutionSnapshot",
    "ExternalExecutionStatus",
    "InMemoryExternalStateReconciler",
    "InMemoryRuntimeStore",
    "RecoveryAssessment",
    "RecoveryCoordinator",
    "RecoveryDecision",
    "make_effect_idempotency_key",
    "ApprovalGrant",
    "HITLInterrupt",
    "HITLInterruptError",
    "HITLController",
    "HITLReason",
    "HITLRequest",
    "HITLStatus",
    "InMemoryHITLStore",
    "TraceCollector",
    "TraceError",
    "TraceEvent",
    "new_trace_id",
    "FAULT_POLICIES",
    "FaultCategory",
    "FaultClassifier",
    "FaultDecision",
    "FaultPolicy",
    "FaultRecoveryController",
    "FaultRecoveryResult",
    "FaultSignal",
    "RecoveryAction",
    "RecoveryUsage",
    "fault_classification_table",
    "PEVRExecutionError",
    "PEVRInterrupt",
    "PEVRGraphRunner",
    "PEVRMetrics",
    "PEVRRequest",
    "PEVRRunReport",
    "PEVRRunResult",
    "PEVRStage",
    "PEVRToolEvidence",
    "PEVRTraceEvent",
]


def __getattr__(name: str):
    """按需载入 P0-13，保持 P0-04/P0-11 纯契约导入不产生循环。"""

    lazy_names = {
        "PEVRExecutionError",
        "PEVRInterrupt",
        "PEVRGraphRunner",
        "PEVRMetrics",
        "PEVRRequest",
        "PEVRRunReport",
        "PEVRRunResult",
        "PEVRStage",
        "PEVRToolEvidence",
        "PEVRTraceEvent",
    }
    if name in lazy_names:
        from agent.runtime import graph, pevr

        value = getattr(graph if name in {"PEVRExecutionError", "PEVRInterrupt", "PEVRGraphRunner"} else pevr, name)
        globals()[name] = value
        return value
    fault_names = {
        "FAULT_POLICIES",
        "FaultCategory",
        "FaultClassifier",
        "FaultDecision",
        "FaultPolicy",
        "FaultRecoveryController",
        "FaultRecoveryResult",
        "FaultSignal",
        "RecoveryAction",
        "RecoveryUsage",
        "fault_classification_table",
    }
    if name in fault_names:
        from agent.runtime import faults

        value = getattr(faults, name)
        globals()[name] = value
        return value
    raise AttributeError(name)
