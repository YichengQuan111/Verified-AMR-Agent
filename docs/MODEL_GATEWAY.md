# P0-03 统一模型网关

## 对业务层的边界

业务代码只依赖 `ModelProviderProtocol`、`ChatMessage` 和结果契约，不接触 GGUF 路径、llama.cpp 启动参数或 OpenAI SDK 客户端。`ChatMessage` 只允许 `system`、`user`、`assistant` 三种角色；Provider 方法不接受 `tools`、`tool_choice`、文件、Shell、任意 `extra_body` 或透传关键字参数。Provider 内部只写入固定的思考控制：Fast 为关闭/预算 0；Smart 的开启思考/预算 512 Token 参数仅为以后重新验收保留，当前 Profile 已硬禁用。调用方不能覆盖这些字段。

## 启动门禁

`ModelProvider.startup()` 请求 `/v1/models`，并要求服务实际暴露的 `id` 与选中 Profile 的固定 alias 完全一致。FastAPI 默认在生命周期启动阶段执行该检查：

- `LLM_PROFILE=fast` 期望 `qwen3.6-fast`；
- `LLM_PROFILE=smart` 当前在任何 HTTP 请求前返回 `MODEL_PROFILE_DISABLED`；
- `LLM_MODEL` 若设置，必须先与所选 Profile 一致；
- `/v1/models` 若同时暴露多个模型，也会因违反 P0 单模型常驻约束而拒绝启动；
- 服务不可达、HTTP 错误或 alias 不匹配都会阻止 API 启动。

在线预检：

```powershell
python .\scripts\check_model_gateway.py
python .\scripts\check_model_gateway.py --profile smart
```

第一条在 Fast 服务在线时必须成功。第二条用于验证禁用门禁，预期非零退出并返回
`MODEL_PROFILE_DISABLED`；它不要求、也不允许先启动 Smart 服务。`enabled` 没有环境
变量覆盖入口，只有收到用户明确指示、修改受审配置并重新完成在线验收后才能恢复。

只有孤立的 API 结构测试可临时设置 `MODEL_GATEWAY_VALIDATE_ON_STARTUP=false`；完整 P0 运行不得关闭门禁，也不能用该开关把 Smart 变相接回生产链。

## 超时和重试

- `LLM_CONNECT_TIMEOUT_SECONDS` 控制连接建立和启动探测；
- `LLM_GENERATION_TIMEOUT_SECONDS` 控制一次生成请求；
- P0-05 节点可通过 `generate_structured(..., max_output_tokens=..., timeout_seconds=...)` 收紧本次结构化生成额度，但不能超过全局配置；
- OpenAI SDK 的隐式网络重试设为 0，避免掩盖超时与重复调用；
- Schema 不合法时仅追加一次受约束修复提示并重试一次；首次生成和修复共享调用级输出 Token 与时间总预算，第二次仍失败即返回稳定的 `MODEL_SCHEMA_VALIDATION_FAILED`。

## 结构化输出

调用方传入 Pydantic 模型，Provider 将其 JSON Schema 发送给 llama.cpp，并使用 `model_validate_json` 验证最终文本。返回值包含：

- 已验证的 Pydantic 对象；
- 实际尝试次数和是否发生修复；
- response id、finish reason、Token usage、system fingerprint；
- `total_usage`：累计首次生成和可能的一次 Schema 修复，供跨节点预算记账；`call.usage` 仍只描述最终响应；
- 启动时记录的 profile、配置 alias、服务 alias、模型创建字段、owner、OpenAI SDK 版本和观测时间。

20 次在线契约冒烟：

```powershell
python .\scripts\smoke_llm_structured.py
```

五个 P0-05 2-shot Prompt 的真实节点冒烟：

```powershell
python .\scripts\smoke_p005_prompts.py --profile fast
```

该脚本依次调用 `understand_goal`、`plan_tasks`、`verify_observation`、`replan`
和 `compose_report`，不仅验证 Pydantic 输出，还检查订单/地点未被静态示例污染、
规划工具未越过本次白名单、观测结论、重规划版本以及最终报告关键事实。

2026-08-21 阶段审计复核时，Fast 又连续完成 3 次独立 `run_id` 的真实 PEVR；Smart
没有启动，禁用门禁在 Fast 服务仍在线时也于网络访问前生效。Smart 的最近一次历史
P0-05 在线结果为 2/5，不能用 alias 检查或单个结构化样例替代五节点验收。

离线契约测试覆盖正确 alias、错误 alias 拒绝启动、超时参数、无工具请求面、一次修复成功和二次失败终止：

```powershell
python -m pytest .\tests\unit\test_model_provider.py -q
```
