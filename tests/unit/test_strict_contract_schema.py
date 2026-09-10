"""小模型适配层：严格合同 Schema 只在开关打开时生效，默认路径逐字节不变。"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from agent.context.contracts import PromptNodeName
from agent.context.prompt_registry import (
    STRICT_CONTRACT_RULE,
    build_strict_understand_definition,
    get_prompt_definition,
)
from agent.planning import ChargingContract, ChargingGoal, TaskContract, TransportContract
from agent.runtime.prefix import SharedPrefixService
from agent.tools import UserRole
from domains.amr_warehouse import TransportOrder
from evals.p018.hard_map import HARD_ENVIRONMENT_REF, snapshot_provider_for_case
from tests.unit.test_p004_contracts import order_payload, task_contract_payload


# 改动前（commit d2bc1a0）在同一解释器下测得的基线摘要；默认路径一旦漂移即失败。
TASK_CONTRACT_SCHEMA_SHA256 = (
    "ef90b29d5d8579084d24fb98241f27f863319339d5222491276945d4d9b8cad0"
)
UNDERSTAND_SYSTEM_PROMPT_SHA256 = (
    "6797b2a0baded35e779d80023e06ce433ae6e2105d11bbd75e7a8973ef160f92"
)
CHARGING_GOAL = {"amr_id": "AMR-01", "charge_station": "C1", "target_percent": 90}


def test_transport_contract_rejects_missing_or_empty_orders() -> None:
    """运输子类把「至少 1 条订单」写进 Schema，Pydantic 在 validator 之前就拒绝。"""

    missing = task_contract_payload()
    missing.pop("orders")
    with pytest.raises(ValidationError):
        TransportContract.model_validate(missing)

    empty = task_contract_payload()
    empty["orders"] = []
    with pytest.raises(ValidationError):
        TransportContract.model_validate(empty)

    with_charging = task_contract_payload()
    with_charging["charging"] = CHARGING_GOAL
    with pytest.raises(ValidationError):
        TransportContract.model_validate(with_charging)

    contract = TransportContract.model_validate(task_contract_payload())
    assert isinstance(contract, TaskContract)
    assert contract.charging is None
    assert not contract.is_charging_contract()


def test_charging_contract_requires_goal_and_forbids_orders() -> None:
    """充电子类要求 charging，且 orders 在 Schema 层只允许空数组。"""

    payload = task_contract_payload()
    payload["orders"] = []
    with pytest.raises(ValidationError):
        ChargingContract.model_validate(payload)

    mixed = task_contract_payload()
    mixed["charging"] = CHARGING_GOAL
    with pytest.raises(ValidationError):
        ChargingContract.model_validate(mixed)

    valid = task_contract_payload()
    valid["orders"] = []
    valid["charging"] = CHARGING_GOAL
    contract = ChargingContract.model_validate(valid)
    assert isinstance(contract, TaskContract)
    assert contract.is_charging_contract()


def test_strict_subclass_schemas_bind_rules_for_grammar() -> None:
    """llama.cpp 只按 required/minItems/maxItems 生成 grammar，规则必须出现在这里。"""

    transport = TransportContract.model_json_schema()
    assert "orders" in transport["required"]
    assert transport["properties"]["orders"]["minItems"] == 1
    assert transport["properties"]["charging"]["type"] == "null"

    charging = ChargingContract.model_json_schema()
    assert "charging" in charging["required"]
    assert "orders" not in charging["required"]
    assert charging["properties"]["orders"]["maxItems"] == 0


def test_task_contract_schema_digest_unchanged() -> None:
    """父类 Schema 是 Qwen 基线的一部分；新增子类不得改动它的任何一个字节。"""

    encoded = json.dumps(TaskContract.model_json_schema(), sort_keys=True).encode("utf-8")
    assert hashlib.sha256(encoded).hexdigest() == TASK_CONTRACT_SCHEMA_SHA256


def test_default_understand_prompt_text_unchanged() -> None:
    """默认模式渲染文本必须与改动前完全一致，Prompt 1.2.0 指纹才不会漂移。"""

    definition = get_prompt_definition(PromptNodeName.UNDERSTAND_GOAL)
    rendered = definition.render_system_prompt()

    assert definition.response_model is TaskContract
    assert hashlib.sha256(rendered.encode("utf-8")).hexdigest() == UNDERSTAND_SYSTEM_PROMPT_SHA256
    assert STRICT_CONTRACT_RULE not in rendered


@pytest.mark.parametrize("response_model", [TransportContract, ChargingContract])
def test_strict_prompt_appends_rule_after_default_text(response_model: type) -> None:
    """追加只发生在末尾：默认文本的共享前缀保持不变，KV 缓存不会失效。"""

    definition = build_strict_understand_definition(response_model)
    rendered = definition.render_system_prompt()
    default_prefix = get_prompt_definition(PromptNodeName.UNDERSTAND_GOAL).render_system_prompt()

    assert definition.prompt_id == "amr.p005.understand_goal"
    assert definition.version == "1.2.0"
    assert rendered.endswith(STRICT_CONTRACT_RULE)
    assert rendered.startswith(default_prefix[:5000])
    # 两组教学示例仍按父类校验，充电子类不会因为示例是运输场景而加载失败。
    assert len(definition.validated_examples()) == 2


class RecordingProvider:
    """记录传入的 response_model，并按该模型返回一份合法合同。"""

    def __init__(self, *, strict: bool) -> None:
        self.settings = SimpleNamespace(
            active_profile=SimpleNamespace(strict_contract_schema=strict)
        )
        self.response_models: list[type] = []

    def generate_structured(
        self,
        messages: Any,
        response_model: type,
        *,
        max_output_tokens: int | None = None,
        timeout_seconds: float | None = None,
    ) -> Any:
        self.response_models.append(response_model)
        payload = task_contract_payload(orders=[order_payload()])
        if response_model is ChargingContract:
            payload["orders"] = []
            payload["charging"] = CHARGING_GOAL
        value = response_model.model_validate(payload)
        return SimpleNamespace(
            value=value,
            attempts=1,
            repaired=False,
            call=SimpleNamespace(
                content=value.model_dump_json(),
                version=SimpleNamespace(served_alias="VeryFast"),
            ),
            total_usage=SimpleNamespace(input_tokens=100, output_tokens=200),
        )


def injected_transport_order() -> TransportOrder:
    """与在线 harness 同一形态的注入订单；understand 之后会被快照真值覆盖。"""

    return TransportOrder(
        order_id="ORDER-001",
        material_id="MAT-001",
        pickup="P4",
        dropoff="S5",
        priority=3,
        release_time=0,
        deadline=120,
        dependencies=[],
    )


def make_request() -> SimpleNamespace:
    return SimpleNamespace(
        run_id="run-strict-001",
        raw_request="把 ORDER-001 从 P1 送到 S1",
        environment_ref=HARD_ENVIRONMENT_REF,
        principal_role=UserRole.OPERATOR,
        principal=None,
        requested_output_tokens=2048,
    )


def run_understand(*, strict: bool, charging: bool) -> RecordingProvider:
    """走完整 understand 路径，确认响应模型确实传到了 provider。"""

    provider = RecordingProvider(strict=strict)
    snapshot_provider = snapshot_provider_for_case(
        amr_id="AMR-01",
        order_id="ORDER-001",
        seed=18021 if charging else 18007,
        pickup=None if charging else "P4",
        dropoff=None if charging else "S5",
        orders=[] if charging else [injected_transport_order()],
        charging=ChargingGoal(**CHARGING_GOAL) if charging else None,
        amr_batteries={"AMR-01": 20.0} if charging else None,
    )
    service = SharedPrefixService(
        provider=provider,
        registry=SimpleNamespace(),
        snapshot_provider=snapshot_provider,
    )
    result = service.understand(make_request())
    assert result.contract.is_charging_contract() is charging
    return provider


def test_default_mode_still_uses_task_contract() -> None:
    """开关关闭时 understand 必须仍绑定注册表默认的 TaskContract。"""

    assert run_understand(strict=False, charging=False).response_models == [TaskContract]
    assert run_understand(strict=False, charging=True).response_models == [TaskContract]


def test_strict_mode_selects_contract_by_injected_charging() -> None:
    """充电与运输由 harness 的注入快照决定，不由模型自选。"""

    assert run_understand(strict=True, charging=False).response_models == [TransportContract]
    assert run_understand(strict=True, charging=True).response_models == [ChargingContract]
