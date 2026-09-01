"""评测专用 TTFT 探针：默认关闭，生产 ModelProvider 仍非流式。"""

from __future__ import annotations

import json

import httpx
import pytest

from evals.p018.run_eval import build_parser
from evals.perf.cli import main as perf_main
from evals.perf.ttft_provider import (
    TtftEvalProvider,
    eval_ttft_requested,
    select_eval_provider,
)
from services.model_gateway.exceptions import ModelGenerationTimeoutError
from services.model_gateway.provider import ModelProvider
from tests.unit.fakes import FakeOpenAIClient
from tests.unit.test_model_provider import MESSAGES, TransportExtraction, make_settings
from tests.unit.test_ttft_metrics import _sse


def test_eval_ttft_requested_default_off() -> None:
    assert eval_ttft_requested(measure_ttft=False, environ={}) is False
    assert eval_ttft_requested(measure_ttft=True, environ={}) is True
    assert eval_ttft_requested(measure_ttft=False, environ={"LLM_EVAL_TTFT": "true"}) is True
    assert eval_ttft_requested(measure_ttft=False, environ={"LLM_EVAL_TTFT": "0"}) is False


def test_select_eval_provider_default_is_production_model_provider() -> None:
    settings = make_settings()
    provider = select_eval_provider(settings, measure_ttft=False)
    assert type(provider) is ModelProvider
    assert not isinstance(provider, TtftEvalProvider)
    enabled = select_eval_provider(settings, measure_ttft=True, client=FakeOpenAIClient(["qwen3.6-fast"]))
    assert isinstance(enabled, TtftEvalProvider)


def test_run_eval_measure_ttft_flag_default_off() -> None:
    args = build_parser().parse_args([])
    assert args.measure_ttft is False
    assert args.llm_only is False
    enabled = build_parser().parse_args(["--measure-ttft", "--llm-only"])
    assert enabled.measure_ttft is True
    assert enabled.llm_only is True


def test_pevr_ttft_cli_without_run_does_not_start_eval() -> None:
    assert perf_main(["pevr-ttft"]) == 0


def test_pevr_ttft_cli_without_run_does_not_start_eval() -> None:
    assert perf_main(["pevr-ttft"]) == 0


def test_llm_case_ids_match_dataset_filter() -> None:
    from evals.p018.dataset import load_dataset
    from evals.perf.llm36 import EXPECTED_LLM_CASES, LLM_CASE_IDS, filter_llm_cases_by_id

    assert len(LLM_CASE_IDS) == EXPECTED_LLM_CASES
    assert len(set(LLM_CASE_IDS)) == EXPECTED_LLM_CASES
    dataset = load_dataset()
    selected = filter_llm_cases_by_id(list(dataset.cases))
    assert [case.case_id for case in selected] == [
        case.case_id for case in dataset.cases if case.case_id in set(LLM_CASE_IDS)
    ]
    assert len(selected) == EXPECTED_LLM_CASES


def test_assert_ttft_rigor_rejects_prefill_fill_and_progress() -> None:
    from evals.perf.cache_compare import _assert_ttft_rigor

    _assert_ttft_rigor(
        {
            "ttft_ms": {"filled_from_prefill": False, "n": 1},
            "invariants": {"pseudo_ttft_from_progress_100": False, "ttft_lte_e2e_violations": []},
        },
        phase="ok",
    )
    with pytest.raises(RuntimeError, match="Prefill"):
        _assert_ttft_rigor({"ttft_ms": {"filled_from_prefill": True}, "invariants": {}}, phase="bad")
    with pytest.raises(RuntimeError, match="progress"):
        _assert_ttft_rigor(
            {
                "ttft_ms": {"filled_from_prefill": False},
                "invariants": {"pseudo_ttft_from_progress_100": True},
            },
            phase="bad",
        )


def test_ttft_eval_provider_records_first_delta_and_skips_sdk_create(tmp_path) -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen["stream"] = payload["stream"]
        seen["response_format"] = payload.get("response_format")
        body = _sse(
            {"choices": [{"delta": {"role": "assistant"}}]},
            {"choices": [{"delta": {"content": '{"pickup":"P1"'}}]},
            {"choices": [{"delta": {"content": ',"dropoff":"S3","quantity":2}'}}]},
            {
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                "timings": {"prompt_ms": 12.0},
            },
            "[DONE]",
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    fake = FakeOpenAIClient(["qwen3.6-fast"], ["should-not-be-used"])
    provider = TtftEvalProvider(
        make_settings(),
        client=fake,
        stream_transport=httpx.MockTransport(handler),
        llama_log_path=None,
    )
    provider.set_case_context("p018-normal-001")
    result = provider.generate_structured(MESSAGES, TransportExtraction)
    assert result.value.pickup == "P1"
    assert result.value.quantity == 2
    assert fake.completions.calls == []
    assert seen["stream"] is True
    assert seen["response_format"]["type"] == "json_object"
    assert len(provider.samples) == 1
    sample = provider.samples[0]
    assert sample.ttft_ms is not None
    assert sample.e2e_ms is not None
    assert sample.ttft_ms <= sample.e2e_ms
    assert sample.prefill_ms == 12.0
    assert sample.ttft_ms != sample.prefill_ms or sample.e2e_ms >= sample.ttft_ms
    assert sample.extra["case_id"] == "p018-normal-001"
    assert sample.extra["eval_path"] == "pevr_online_stream"
    metrics_path = provider.write_ttft_artifacts(tmp_path)
    payload = json.loads(metrics_path.read_text(encoding="utf-8"))
    assert payload["kind"] == "amr.pevr_ttft.v1"
    assert payload["production_stream"] is False
    assert payload["summary"]["ttft_ms"]["n"] == 1
    assert (tmp_path / "pevr_ttft_samples.jsonl").is_file()


def test_ttft_eval_provider_timeout_is_recorded_and_raised() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("still prefilling", request=request)

    fake = FakeOpenAIClient(["qwen3.6-fast"], ["unused"])
    provider = TtftEvalProvider(
        make_settings(generation_timeout_seconds=1.0),
        client=fake,
        stream_transport=httpx.MockTransport(handler),
        llama_log_path=None,
    )
    with pytest.raises(ModelGenerationTimeoutError):
        provider.generate_structured(MESSAGES, TransportExtraction)
    assert len(provider.samples) == 1
    assert provider.samples[0].ttft_ms is None
    assert provider.samples[0].ttft_missing_reason == "timeout"
    assert provider.samples[0].prefill_ms is None
