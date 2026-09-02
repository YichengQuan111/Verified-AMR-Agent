# P0-17 Trace、受控验证与证据报告

## 交付边界

P0-17 在 P0-12～P0-16 的工具、PEVR、Checkpoint、故障和安全边界之上增加三层能力：

1. TraceEvent 是运行事实索引，按一个 trace_id 在 run_id 内严格递增；
2. FixedVerificationRunner 只接受预注册的 CTest、pytest、smoke 和仿真入口；
3. VerificationLogParser 与 VerificationReportGenerator 把真实退出码、日志和证据引用收口为 JSON/Markdown 报告。

本专题、测试和 JSON Schema 是文档/契约交付，本步无核心代码注释需求；Python 核心实现同步包含中文 docstring 和关键安全分支说明。build/、__pycache__/、.pytest_cache/、tmp/ 和系统临时报告均为自动生成物，不属于源码交付。

## TraceEvent 字段

agent.runtime.trace.TraceEvent 的公共字段如下：

| 字段 | 作用 |
|---|---|
| trace_id、run_id、sequence | 关联一次链路、业务运行和严格事件顺序；恢复时复用同一 Trace |
| event_type、status、node、task_id | 定位节点/模型/工具/验证阶段及失败任务；task 是 task_id 的读取别名 |
| model_version | 模型 served alias；模型降级/预算拒绝时允许为空 |
| prompt_id、prompt_version | P0-05 具名 Prompt 和版本 |
| tool_name、tool_version | 工具注册名和执行版本 |
| input_tokens、output_tokens、total_tokens | 单次模型调用 Token 增量；total_tokens 可由前两项重算 |
| started_at、finished_at、latency_ms | 带时区时间和可重算毫秒延迟 |
| parameters_digest、input_digest、output_digest | 参数/输入/输出的 SHA-256，不复制敏感正文 |
| error | category/code/message/retryable/details，失败、超时和拒绝事件必填 |
| evidence_refs、metadata | 工具、Prompt、仿真、日志、报告 URI 及受控审计元数据 |

PEVR 成功闭环会记录八个 node 事件、模型节点事件和每次工具事件；P0-14
InMemoryRuntimeStore 与 PostgresRuntimeStore 都复用 events 事实表/适配器。节点
失败在异常向上抛出前写入带阶段、任务、Fault 和证据的失败事件。理解节点的模型事件
发生在 runs 创建前时由 PostgreSQL Store 暂存，ensure_run 成功后按序补写。

## 受控验证入口

工具名仍为 run_verification_suite，可接受的 suite_id/case_id 只有下表内容：

| Suite | 已注册 case | 实际入口 |
|---|---|---|
| p0_12 / p0-12 | contract、security、idempotency、cpp_adapter | 固定 pytest 文件和固定 node ID |
| p0_python | all | 当前项目解释器执行 pytest -q -p no:cacheprovider |
| p0_cpp | all | 解析受信 PATH 中的 ctest，执行 --test-dir build/cpp --output-on-failure |
| p0_smoke | all | 固定 scripts/run_smoke.ps1 -SkipCpp，无交互、无 shell 拼接 |
| p0_simulation / p0_sim | all（内部 case normal） | python -m services.validation.simulation_entry |

调用方不能传 executable、脚本路径、shell 片段、pytest -k 表达式或任意测试表达式。
未知 suite/case 在启动子进程前拒绝；所有子进程使用 argv 列表、shell=False、固定
工作目录和整体 deadline。仿真入口固定 seed/plan/仿真 ID，stdout 输出真实
SimulationResult JSON，非 completed 状态使用非零退出码。

## 日志解析与报告

VerificationLogParser 只读取固定入口已经采集的 stdout、stderr、退出码和超时事实，
不会执行日志内容。每个 case 输出：

- status：passed、failed 或 timeout；
- failure_type：assertion、schema、timeout、permission、unsafe_plan、
  simulation_blocked、tool_error、infrastructure 或 unknown；
- task_id、tool_name、parameters_digest；
- stdout/stderr digest；
- 最多四个带行号的 log://.../stdout#Lx 或 stderr#Lx 位置，以及仿真事件引用；
- summary 短摘要。

VerificationReportGenerator 从逐 case 结果重新计算状态、计数、证据集合和 SHA-256
report_digest，拒绝空 case，且契约拒绝“非零退出码但 status=passed”的伪造结果。
生成的 JSON 和 Markdown 共享 report_id、report_digest、trace_id 与证据引用；
Markdown 明确写出结论来自固定入口退出码和逐 case 解析结果。

## 验证命令

    python -m pytest tests\unit\test_p017_trace.py tests\unit\test_p017_validation.py -q -p no:cacheprovider
    python -m pytest tests\unit\test_p012_tools.py tests\unit\test_p013_pevr.py tests\unit\test_p014_checkpoint.py -q -p no:cacheprovider
    python scripts\export_schemas.py
    & '.\scripts\run_smoke.ps1'

真实仿真入口可独立执行：

    python -m services.validation.simulation_entry

该入口在 P0-17 验收中实际返回退出码 0，结果 status=completed、
validation_result.status=valid、error_count=0；完整 stdout 是运行证据，不登记为源码文件。

## 限制

- 受控 suite 只报告固定入口能够观察到的日志；未知日志词保持 unknown，不会猜测更具体故障。
- CTest 需要 build/cpp 已由项目构建流程准备；入口缺失返回受控 unavailable，不会降级为任意命令。
- Trace 保存摘要和引用，不保存完整 Prompt/日志正文；需要原文时按 evidence URI 读取原始产物。
- P0-17 不新增 Alembic 表；PostgreSQL 复用 P0-06 events 表，Trace payload 保存为 JSONB。
