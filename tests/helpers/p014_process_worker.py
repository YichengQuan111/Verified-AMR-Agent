"""P0-14 真杀进程测试 worker。

worker 在 dispatch 已把真实仿真快照提交 PostgreSQL、但 Effect Ledger 尚未完成
的精确窗口调用 ``os._exit``。父 pytest 进程随后用全新的 Engine/Runner 恢复，
验证该窗口不会产生第二次派发。该故障钩子仅位于 tests/helpers，不进入生产链。
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.runtime.checkpoint import canonical_json_digest  # noqa: E402
from agent.runtime.graph import PEVRGraphRunner  # noqa: E402
from agent.runtime.pevr import PEVRRequest  # noqa: E402
from agent.tools import ToolName, ToolResult, UserRole, build_tool_registry  # noqa: E402
from services.application import PostgresRuntimeStore  # noqa: E402
from services.config.settings import AppSettings  # noqa: E402
from services.persistence import create_database_runtime  # noqa: E402
from tests.unit.test_p013_pevr import (  # noqa: E402
    ENVIRONMENT_REF,
    _FakeProvider,
    _FakeRegistry,
    _contract,
    _now,
    _plan,
)


KILL_EXIT_CODE = 73


class KillAfterExternalWriteStore(PostgresRuntimeStore):
    """只在测试子进程中，于外部快照提交后立即终止整个进程。"""

    def put(self, run_id, snapshot, *, idempotency_key=None):
        super().put(run_id, snapshot, idempotency_key=idempotency_key)
        os._exit(KILL_EXIT_CODE)


class ProcessRecoveryRegistry:
    """复用 P0-13 业务 fake，但让 dispatch/query 使用真实 PostgreSQL 状态边界。"""

    def __init__(self, store: PostgresRuntimeStore, run_id: str) -> None:
        self._store = store
        self._fake = _FakeRegistry(run_id)
        self._real = build_tool_registry(execution_store=store)
        self.dispatch_calls = 0

    def specs(self):
        return self._real.specs()

    def get(self, tool_name):
        return self._real.get(tool_name)

    def execute(
        self,
        tool_name: ToolName | str,
        arguments: Mapping[str, Any],
        *,
        role: UserRole,
        call_id: str,
        idempotency_key: str | None = None,
    ) -> ToolResult:
        name = tool_name if isinstance(tool_name, ToolName) else ToolName(tool_name)
        if name is ToolName.QUERY_EXECUTION_STATE:
            return self._real.execute(
                name,
                arguments,
                role=role,
                call_id=call_id,
                idempotency_key=idempotency_key,
            )
        result = self._fake.execute(name, arguments, role=role, call_id=call_id)
        if name is not ToolName.DISPATCH_SIMULATION:
            return result

        self.dispatch_calls += 1
        definition = self._real.get(name)
        parsed = definition.input_model.model_validate(arguments)
        input_digest = canonical_json_digest(parsed)
        simulation_digest = canonical_json_digest(
            {
                "plan": arguments.get("plan"),
                "seed": arguments.get("seed"),
                "until_time": arguments.get("until_time"),
            }
        )
        simulation_id = f"simulation-{simulation_digest[:24]}"
        output = dict(result.output)
        output["simulation_id"] = simulation_id
        self._store.put(
            simulation_id,
            output,
            idempotency_key=idempotency_key,
        )
        return result.model_copy(
            update={
                "output": output,
                "effect_id": simulation_id,
                "input_digest": input_digest,
                "output_digest": canonical_json_digest(output),
                "idempotency_key": idempotency_key,
            }
        )


def run_worker(run_id: str) -> int:
    """执行到故障窗口；正常返回表示故障钩子没有命中，应判测试失败。"""

    runtime = create_database_runtime(AppSettings().database)
    try:
        contract = _contract()
        store = KillAfterExternalWriteStore(runtime.session_factory, clock=_now)
        PEVRGraphRunner(
            _FakeProvider(contract, _plan(contract), run_id),
            registry=ProcessRecoveryRegistry(store, run_id),
            checkpoint_store=store,
            clock=_now,
        ).run(
            PEVRRequest(
                run_id=run_id,
                raw_request="把 MAT-001 从 P1 运到 S3",
                environment_ref=ENVIRONMENT_REF,
                seed=7,
                approval_granted=True,
            )
        )
    finally:
        runtime.dispose()
    return 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    return run_worker(args.run_id)


if __name__ == "__main__":
    raise SystemExit(main())
