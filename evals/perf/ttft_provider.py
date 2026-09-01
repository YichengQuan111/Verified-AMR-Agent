"""评测专用 TTFT 探针：只替换在线 Harness 的生成通道，不改生产 ``stream=false``。

打开方式（默认关闭）：

- ``python -m evals.p018.run_eval ... --measure-ttft``
- 或环境变量 ``LLM_EVAL_TTFT=true``（仅 ``run_eval`` CLI 读取，Harness 构造参数不读环境）
- 或 ``python -m evals.perf pevr-ttft --run``

生产 ``ModelProvider._request_completion`` 仍固定 ``stream: False``。
本类覆盖 ``_request_completion``，用 ``StreamingChatClient`` 发 ``stream=true``，
记录客户端首个非空生成 delta，再把拼接文本交回 ``generate_structured`` 的
Schema 校验 / 一次修复循环。业务节点看不到流式细节。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from evals.perf.client import StreamingChatClient
from evals.perf.contracts import LatencySample, metric_definitions_for_report
from evals.perf.llama_log import LlamaLogCursor
from evals.perf.stats import summarize_samples
from services.config.settings import ModelGatewaySettings
from services.model_gateway.contracts import ChatMessage, ModelCallResult, TokenUsage
from services.model_gateway.exceptions import (
    EmptyModelResponseError,
    ModelGenerationError,
    ModelGenerationTimeoutError,
)
from services.model_gateway.provider import ModelProvider


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LLAMA_LOG = PROJECT_ROOT / "tmp" / "llama-server.err.log"
TRUE_ENV = frozenset({"1", "true", "yes", "on"})


def eval_ttft_requested(
    *,
    measure_ttft: bool = False,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """评测开关。默认关闭；CLI 或 ``LLM_EVAL_TTFT`` 才打开。"""

    if measure_ttft:
        return True
    env = environ if environ is not None else os.environ
    raw = str(env.get("LLM_EVAL_TTFT") or "").strip().lower()
    return raw in TRUE_ENV


def select_eval_provider(
    settings: ModelGatewaySettings,
    *,
    measure_ttft: bool = False,
    client: Any | None = None,
    stream_transport: Any | None = None,
    llama_log_path: Path | None = DEFAULT_LLAMA_LOG,
) -> ModelProvider:
    """在线 Harness 选 Provider：只认布尔开关，不读进程环境。"""

    if not measure_ttft:
        return ModelProvider(settings, client=client)
    return TtftEvalProvider(
        settings,
        client=client,
        stream_transport=stream_transport,
        llama_log_path=llama_log_path,
    )


class TtftEvalProvider(ModelProvider):
    """在线评测专用 Provider：startup 仍走生产门禁，生成走流式 TTFT。"""

    def __init__(
        self,
        settings: ModelGatewaySettings,
        client: Any | None = None,
        *,
        stream_transport: Any | None = None,
        llama_log_path: Path | str | None = DEFAULT_LLAMA_LOG,
    ) -> None:
        super().__init__(settings, client=client)
        cursor = LlamaLogCursor(llama_log_path) if llama_log_path else None
        profile = settings.active_profile
        self._stream_client = StreamingChatClient(
            base_url=settings.base_url,
            api_key=settings.api_key.get_secret_value(),
            model=profile.alias,
            temperature=profile.temperature,
            top_p=profile.top_p,
            top_k=profile.top_k,
            reasoning_enabled=profile.reasoning_enabled,
            reasoning_budget=profile.reasoning_budget_tokens,
            connect_timeout_seconds=settings.connect_timeout_seconds,
            timeout_seconds=settings.generation_timeout_seconds,
            transport=stream_transport,
            llama_log_cursor=cursor,
        )
        self.samples: list[LatencySample] = []
        self._case_id: str | None = None

    def set_case_context(self, case_id: str | None) -> None:
        """在线 Harness 逐例绑定 case_id，写入样本 extra，不进入 Prompt。"""

        self._case_id = case_id

    def _request_completion(
        self,
        messages: Sequence[ChatMessage],
        *,
        response_format: dict[str, Any] | None = None,
        max_output_tokens: int | None = None,
        timeout_seconds: float | None = None,
    ) -> ModelCallResult:
        """流式完成一次生成，记录 TTFT 后再交给上层 Schema 校验。"""

        if max_output_tokens is not None and max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be greater than zero")
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        effective_output_tokens = min(
            self.settings.max_output_tokens,
            max_output_tokens or self.settings.max_output_tokens,
        )
        effective_timeout_seconds = min(
            self.settings.generation_timeout_seconds,
            timeout_seconds or self.settings.generation_timeout_seconds,
        )
        version = self.startup()
        sample = self._stream_client.complete_stream(
            [message.model_dump() for message in messages],
            cache_prompt=self.settings.prompt_cache_enabled,
            kind="measured",
            max_tokens=effective_output_tokens,
            timeout_seconds=effective_timeout_seconds,
            response_format=response_format,
        )
        sample.extra["case_id"] = self._case_id
        sample.extra["eval_path"] = "pevr_online_stream"
        self.samples.append(sample)

        if sample.exclusion_reason == "timeout":
            raise ModelGenerationTimeoutError(
                f"model generation exceeded {effective_timeout_seconds}s"
            )
        if sample.outcome == "failed" or (
            sample.http_status is not None and sample.http_status >= 400
        ):
            raise ModelGenerationError(
                sample.error or f"model generation failed with HTTP {sample.http_status}"
            )
        content = sample.generated_text
        if not isinstance(content, str) or not content.strip():
            raise EmptyModelResponseError("model response contains no text content")

        finish_reason = None
        extra_finish = sample.extra.get("finish_reason")
        if isinstance(extra_finish, str):
            finish_reason = extra_finish
        return ModelCallResult(
            content=content,
            response_id=sample.request_id,
            finish_reason=finish_reason,
            usage=TokenUsage(
                input_tokens=sample.prompt_tokens,
                output_tokens=sample.completion_tokens,
                total_tokens=(
                    None
                    if sample.prompt_tokens is None or sample.completion_tokens is None
                    else sample.prompt_tokens + sample.completion_tokens
                ),
                cached_input_tokens=sample.cached_input_tokens,
            ),
            version=version,
        )

    def write_ttft_artifacts(self, output_dir: Path) -> Path:
        """把逐次样本和百分位写到评测输出目录；不覆盖 P0-18 正式报告文件名。"""

        output_dir.mkdir(parents=True, exist_ok=True)
        summary = summarize_samples(self.samples)
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "kind": "amr.pevr_ttft.v1",
            "definitions": metric_definitions_for_report(),
            "eval_path": "pevr_online_stream",
            "production_stream": False,
            "sample_count": len(self.samples),
            "summary": summary,
            "samples": [item.to_dict() for item in self.samples],
            "note": (
                "TTFT 来自评测专用 stream=true 探针，不是生产 ModelProvider。"
                "Prefill 仍来自 timings/llama 日志。案例墙钟见 p018_online_eval.json。"
            ),
        }
        metrics_path = output_dir / "pevr_ttft_metrics.json"
        metrics_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        samples_path = output_dir / "pevr_ttft_samples.jsonl"
        with samples_path.open("w", encoding="utf-8") as handle:
            for item in self.samples:
                handle.write(json.dumps(item.to_dict(), ensure_ascii=False) + "\n")
        return metrics_path


__all__ = [
    "TtftEvalProvider",
    "eval_ttft_requested",
    "select_eval_provider",
]
