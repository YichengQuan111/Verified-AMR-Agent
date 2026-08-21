"""P0-20 部署契约测试。

这些测试只读取版本化的 Compose、启动脚本和交付文档，不启动外部服务；这样可以在
Docker 尚未安装或模型未启动时，先阻断“漏编排、错误依赖或误把 Smart 写入启动链”的
回归。真实容器健康、模型网关和端到端闭环仍由 P0-20 验收命令负责。
"""

from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _read_text(relative_path: str) -> str:
    """以 UTF-8 读取交付文件，避免 Windows 默认编码改变中文契约断言。"""

    return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


def test_compose_exposes_only_p020_services_and_health_gates() -> None:
    """Compose 必须只编排 P0-20 的三项服务，并按健康状态启动 API。"""

    compose = yaml.safe_load(_read_text("compose.yaml"))
    services = compose["services"]

    assert set(services) == {"postgres", "qdrant", "api"}
    assert services["postgres"]["healthcheck"]["test"]
    assert services["qdrant"]["healthcheck"]["test"]

    api = services["api"]
    assert api["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert api["depends_on"]["qdrant"]["condition"] == "service_healthy"
    assert api["environment"]["LLM_PROFILE"] == "fast"
    assert api["environment"]["LLM_MODEL"] == "qwen3.6-fast"
    assert "false" in api["environment"]["MODEL_GATEWAY_VALIDATE_ON_STARTUP"]
    assert "host.docker.internal:host-gateway" in api["extra_hosts"]


def test_p020_image_and_start_script_keep_fast_on_host() -> None:
    """镜像不得携带模型；宿主启动器只能可选启动 Fast，Smart 仍是禁用边界。"""

    dockerfile = _read_text("infra/Dockerfile.api")
    api_lock = _read_text("infra/requirements.api.lock")
    api_requirements = "\n".join(
        line for line in api_lock.splitlines() if not line.lstrip().startswith("#")
    )
    dockerignore = _read_text(".dockerignore")
    startup = _read_text("scripts/start_local.ps1")

    assert "gguf" not in dockerfile.lower()
    assert "requirements.api.lock" in dockerfile
    assert "sentence-transformers" not in api_requirements
    assert "torch" not in api_requirements.lower()
    assert "E:\\Llama.cpp\\start-qwen3.6-agent.cmd" in startup
    assert "start-qwen3.8-agent.cmd" not in startup
    assert "*.gguf" in dockerignore
    assert "*.safetensors" in dockerignore


def test_p020_delivery_documents_are_linked_and_boundary_explicit() -> None:
    """交付文档必须存在，并明确真实入口、离线评测和 Smart 限制。"""

    required = (
        "docs/ARCHITECTURE.md",
        "docs/API.md",
        "docs/SERVICES_STARTUP.md",
        "docs/TEST_REPORT.md",
        "docs/DEMO_SCRIPT.md",
        "docs/RESUME_FACTS.md",
    )
    for relative_path in required:
        assert (PROJECT_ROOT / relative_path).is_file(), relative_path

    readme = _read_text("README.md")
    assert "P0-20" in readme
    assert "Smart" in readme
    assert "offline_trace_replay" in readme
