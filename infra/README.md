# Infrastructure

P0-20 的部署资产如下：

- `Dockerfile.api`：构建非 root FastAPI 容器镜像，使用 `requirements.api.lock`；不复制本地 GGUF/Embedding 权重，也不安装模型运行时。
- 根目录 `compose.yaml`：启动 `api`、`postgres`、`qdrant`；数据库和 Qdrant 使用命名卷，API 启动时执行幂等前向迁移。
- `scripts/start_local.ps1`：启动 Compose 基础栈并等待健康检查；传入 `-StartFast` 时走仓库内 `scripts/start_fast_secure.ps1`，不会启动 Smart。

Qwen3.6 Fast 和 Qwen3.8 Smart 不进入 Compose。Fast 必须由宿主机 `.\scripts\start_local.ps1 -StartFast` 启动（artifact 根目录见 [`docs/LOCAL_ENV.md`](../docs/LOCAL_ENV.md)）；Smart 仍因历史在线 P0-05 仅 2/5 而硬禁用。Compose API 默认关闭启动期模型门禁以允许基础健康检查，真实模型联调仍须通过主机 `scripts/check_model_gateway.py --profile fast`。
