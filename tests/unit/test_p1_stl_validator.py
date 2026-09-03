"""P1-1 STL 第二判定层：Python 侧契约、固定 argv、真实 C++ CLI 与一致性 harness。

与 ``test_p011_simulator.py`` 一样直接调用仓库内已构建的
``fleet_plan_validator_cli.exe``；C++ 语义细节由 CTest（``stl_*``）覆盖，这里只
验证跨语言边界：输出能被 ``ValidationResponse`` 解析、gate 模式拒绝会以稳定错误码
进入 errors、工具层把鲁棒度摘要写入审计元数据、harness 的变异/合成场景在真实
CLI 上保持规则层与 STL 层布尔一致。
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest
from pydantic import ValidationError

from agent.runtime.pevr import PEVRMetrics
from agent.tools.cpp_client import CppProgram, FixedCppJsonClient, STL_SPECIFICATION_RELATIVE_PATH
from agent.tools.schemas import STLMonitorOutput, ValidationResponse
from evals.stl_consistency import harness
from services.amr_simulator.validator import FleetPlanValidatorClient

CLIENT = FixedCppJsonClient()
EXECUTABLE = CLIENT.executable_path(CppProgram.FLEET_PLAN_VALIDATOR)
SPEC_PATH = CLIENT.stl_specification_path

pytestmark = pytest.mark.skipif(not EXECUTABLE.is_file(), reason="需要先构建 build/cpp 的 fleet_plan_validator_cli.exe")


def _run_cli(plan: dict, *arguments: str) -> tuple[int, dict]:
    completed = subprocess.run(
        [str(EXECUTABLE), "--validate", *arguments],
        input=json.dumps(plan, ensure_ascii=False, sort_keys=True, allow_nan=False),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30.0,
        check=False,
        shell=False,
        cwd=str(CLIENT.repository_root),
    )
    return completed.returncode, json.loads(completed.stdout)


def test_fixed_spec_path_is_repository_internal() -> None:
    """规约路径固定在仓库内，工具层与仿真门禁使用同一份文件。"""

    assert STL_SPECIFICATION_RELATIVE_PATH == Path("config") / "stl" / "fleet_plan_stl_spec.json"
    assert SPEC_PATH.is_file()
    assert FleetPlanValidatorClient().stl_specification == SPEC_PATH
    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    assert spec["enforcement"] == "gate"
    assert [item["id"] for item in spec["formulas"]] == [
        "time_window",
        "battery_safety",
        "traffic_rules",
        "load_capacity",
        "fleet_separation",
        "workstation_capacity",
        "dependency_precedence",
        "low_battery_charging",
    ]


def test_cli_output_with_stl_parses_and_reports_robustness() -> None:
    """真实 CLI 输出必须通过 ValidationResponse，且 STL 报告字段完整。"""

    plan = harness.canonical_plan(harness.synthetic_base())
    exit_code, raw = _run_cli(plan, "--stl-spec", str(SPEC_PATH))
    assert exit_code == 0
    response = ValidationResponse.model_validate(raw)
    assert response.valid and response.stl is not None
    assert response.stl.status == "satisfied" and response.stl.enforcement == "gate"
    assert response.stl.formula_count == 8 and response.stl.instance_count == 15
    scope_key = {"order": "order_id", "amr": "amr_id", "pair": "amr_id", "station": "station_id", "dependency": "order_id"}
    by_formula = {
        (item.formula_id, getattr(item.scope, scope_key[item.scope.kind])): item for item in response.stl.results
    }
    assert by_formula[("time_window", "ORDER-001")].robustness == 18.0
    assert by_formula[("battery_safety", "AMR-01")].robustness == 80.0
    assert by_formula[("load_capacity", "AMR-01")].weakest_time == 1
    assert response.stl.min_robustness == 0.0 and response.stl.min_robustness_formula_id == "traffic_rules"

    exit_code, plain = _run_cli(plan)
    assert exit_code == 0
    assert ValidationResponse.model_validate(plain).stl is None


def test_gate_mode_rejects_with_stable_error_code(tmp_path: Path) -> None:
    """只有 STL 会违反的规约在 gate 模式下必须让计划 invalid；shadow 只记录。"""

    plan = harness.canonical_plan(harness.synthetic_base())
    strict = {
        "schema_version": "1.0",
        "spec_id": "strict",
        "spec_version": "test",
        "enforcement": "gate",
        "formulas": [
            {"id": "strict_battery", "scope": "amr", "description": "d", "formula": "G(battery >= 99)", "rule_codes": [], "warn_below": None}
        ],
    }
    gate_path = tmp_path / "gate.json"
    gate_path.write_text(json.dumps(strict), encoding="utf-8")
    exit_code, raw = _run_cli(plan, "--stl-spec", str(gate_path))
    assert exit_code == 0
    response = ValidationResponse.model_validate(raw)
    assert not response.valid and response.status == "invalid"
    codes = {item.code for item in response.errors}
    assert codes == {"stl_specification_violated"}
    evidence = response.errors[0]
    assert evidence.constraint == "stl_specification" and "strict_battery" in evidence.message
    assert evidence.observed == -1.0 and evidence.limit == 0.0 and evidence.time == 2
    assert evidence.coordinate is not None

    shadow_path = tmp_path / "shadow.json"
    shadow_path.write_text(json.dumps({**strict, "enforcement": "shadow"}), encoding="utf-8")
    exit_code, raw = _run_cli(plan, "--stl-spec", str(shadow_path))
    shadow = ValidationResponse.model_validate(raw)
    assert exit_code == 0 and shadow.valid and shadow.stl is not None and shadow.stl.status == "violated"

    # 规约文件损坏时是契约错误（退出码 2），不能退化成只跑规则层。
    broken_path = tmp_path / "broken.json"
    broken_path.write_text(json.dumps({**strict, "formulas": [{**strict["formulas"][0], "formula": "G(battery >=)"}]}), encoding="utf-8")
    exit_code, raw = _run_cli(plan, "--stl-spec", str(broken_path))
    assert exit_code == 2 and raw["error"]["code"] == "invalid_stl_specification"
    exit_code, raw = _run_cli(plan, "--stl-spec", str(tmp_path / "missing.json"))
    assert exit_code == 2 and raw["error"]["code"] == "invalid_stl_specification"


def test_validation_response_rejects_inconsistent_stl_summary() -> None:
    """Python 契约拒绝与逐条结果矛盾的 STL 汇总，以及 gate 违反却 valid 的输出。"""

    base = {
        "spec_id": "s",
        "spec_version": "v",
        "enforcement": "gate",
        "status": "satisfied",
        "satisfied": True,
        "skip_reason": None,
        "formula_count": 1,
        "instance_count": 0,
        "violated_count": 0,
        "narrow_pass_count": 0,
        "min_robustness": None,
        "min_robustness_formula_id": None,
        "min_robustness_scope": None,
        "results": [],
    }
    STLMonitorOutput.model_validate(base)
    with pytest.raises(ValidationError):
        STLMonitorOutput.model_validate({**base, "violated_count": 1})
    with pytest.raises(ValidationError):
        STLMonitorOutput.model_validate({**base, "status": "skipped", "satisfied": True})
    with pytest.raises(ValidationError):
        ValidationResponse.model_validate(
            {
                "schema_version": "1.0",
                "ruleset_version": "p0-10.v1",
                "status": "valid",
                "valid": True,
                "error_count": 0,
                "errors": [],
                "stl": {**base, "status": "violated", "satisfied": False, "instance_count": 1, "violated_count": 1, "results": [
                    {
                        "formula_id": "f",
                        "scope": {"kind": "amr", "order_id": "", "related_order_id": "", "amr_id": "A", "related_amr_id": "", "station_id": ""},
                        "satisfied": False,
                        "robustness": -1.0,
                        "weakest_time": 0,
                        "coordinate": None,
                        "related_coordinate": None,
                        "vacuous": False,
                        "narrow_pass": False,
                    }
                ]},
            }
        )


def test_pevr_metrics_keep_stl_fields_optional() -> None:
    """旧报告没有 STL 字段仍可解析；新字段只做记录。"""

    metrics = PEVRMetrics(
        graph_stage_count=8,
        model_call_count=1,
        tool_call_count=1,
        successful_tool_call_count=1,
        plan_task_count=1,
        validator_error_count=0,
        retrieval_result_count=0,
        completed_order_count=1,
        route_count=1,
        simulation_status="completed",
        simulation_end_time=1,
        total_tool_duration_ms=1,
    )
    assert metrics.stl_status is None and metrics.stl_narrow_pass_count == 0
    with pytest.raises(ValidationError):
        PEVRMetrics(**{**metrics.model_dump(), "stl_narrow_pass_count": -1})


def test_harness_synthetic_and_mutations_are_consistent() -> None:
    """合成冲突场景与全部变异在真实 CLI 上必须规则层/STL 层布尔一致。"""

    cli = harness.ValidatorCli()
    formulas = cli.describe_spec()["formulas"]
    records = [harness.check_plan(name, "synthetic", harness.canonical_plan(plan), cli, formulas) for name, plan in harness.synthetic_scenarios()]
    assert [record.plan_id for record in records if not record.rules_valid] == [
        "synthetic_dependency_time_order",
        "synthetic_workstation_capacity",
        "synthetic_safety_distance",
        "synthetic_vertex_conflict",
        "synthetic_swap_edge",
    ]
    assert all(record.plan_consistent for record in records)
    assert all(not record.gate_valid for record in records if not record.rules_valid)

    base = harness.canonical_plan(harness.synthetic_base())
    applied = 0
    for name, mutation in harness.MUTATIONS.items():
        mutated = mutation(base)
        if mutated is None:
            continue
        applied += 1
        record = harness.check_plan(name, "case_mutation", harness.canonical_plan(mutated), cli, formulas, mutation=name)
        assert not record.rules_valid, name
        assert record.plan_consistent, (name, record.rule_codes, record.violated_formulas)
    assert applied >= 10
