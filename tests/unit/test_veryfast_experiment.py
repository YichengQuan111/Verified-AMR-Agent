"""模型切换实验的隔离、失败留档和历史公平性反例。"""
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from evals.perf.veryfast import ObservedCompletions, choose_candidate, compare_inputs, make_settings, server_argv, stats
from services.config.settings import ModelProfileSettings


def test_reasoning_budget_allows_unrestricted_but_not_invalid_negative():
    """-1 只取消思考子预算，不改变总输出的硬上限。"""
    p = ModelProfileSettings(alias="VeryFast", context_window=16384, reasoning_enabled=True, reasoning_budget_tokens=-1)
    assert p.reasoning_budget_tokens == -1
    with pytest.raises(ValidationError):
        ModelProfileSettings(alias="VeryFast", context_window=16384, reasoning_budget_tokens=-2)


def test_experiment_settings_leave_fast_smart_and_budgets_unchanged():
    sampling = {"temperature": .1, "top_p": .95, "top_k": 20}
    s = make_settings(sampling, "http://127.0.0.1:8081/v1", "x" * 40)
    assert s.model_gateway.active_alias == "VeryFast"
    assert s.model_gateway.profiles["fast"].reasoning_enabled is False
    assert s.model_gateway.profiles["smart"].enabled is False
    assert s.model_gateway.max_output_tokens == 4096
    assert s.model_gateway.generation_timeout_seconds == 120
    assert s.model_gateway.active_profile.context_window == 16384


def test_candidate_selection_excludes_failed_and_prefers_f16_within_noise_band():
    rows = [dict(candidate={"kv":"q8_0"}, benchmark={"projected_4096_ms":100}),
            dict(candidate={"kv":"f16"}, benchmark={"projected_4096_ms":104}),
            dict(candidate={"kv":"f16"}, error="OOM")]
    assert choose_candidate(rows) is rows[1]
    with pytest.raises(ValueError):
        choose_candidate([rows[2]])


def test_launch_is_single_slot_loopback_and_no_qwen_moe_offload():
    args = server_argv("server.exe", "model with spaces.gguf", dict(kv="f16", batch=2048, ubatch=512, threads=6, threads_batch=14))
    assert args[args.index("--model") + 1] == "model with spaces.gguf"
    assert args[args.index("--host") + 1] == "127.0.0.1"
    assert args[args.index("--parallel") + 1] == "1"
    assert args[args.index("--reasoning-budget") + 1] == "-1"
    assert "--n-cpu-moe" not in args


def test_failed_request_is_recorded_without_secret_or_fake_ttft(tmp_path):
    def fail(**kwargs):
        raise RuntimeError("do-not-log-secret")
    observer = ObservedCompletions(SimpleNamespace(create=fail), tmp_path / "requests.jsonl")
    observer.case_id = "failure-case"
    with pytest.raises(RuntimeError):
        observer.create(messages=[{"role":"user", "content":"x"}], max_tokens=42, stream=False)
    row = observer.rows[0]
    assert row["status"] == "error" and row["case_id"] == "failure-case"
    assert row["ttft_ms"] is None and row["wall_ms"] >= 0
    assert "do-not-log-secret" not in (tmp_path / "requests.jsonl").read_text()


def test_fairness_rejects_changed_dataset():
    current = dict(input_files={k:{"sha256":"same"} for k in ["dataset", "map", "amrs", "orders"]},
                   case_seed_digest="seed", prompt_files={}, prompt_versions={}, tool_spec_versions={}, model={"context_window":16384})
    from copy import deepcopy
    old = {"reproducibility":deepcopy(current)}
    assert all(compare_inputs(old, current).values())
    current["input_files"]["dataset"]["sha256"] = "changed"
    with pytest.raises(ValueError):
        compare_inputs(old, current)


def test_latency_empty_is_missing_not_zero():
    assert stats([])["p50"] is None
    assert stats([10, 20])["p50"] == 15


def test_harness_explicit_provider_does_not_construct_fast(monkeypatch):
    """注入 VeryFast 后不能暗中再创建/启动 Qwen Provider。"""
    from evals.p018 import online
    from evals.p018.dataset import load_config, load_dataset, DEFAULT_DATASET_PATH
    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected default Provider")
    monkeypatch.setattr(online, "select_eval_provider", forbidden)
    provider = object()
    settings = make_settings({"temperature":.1, "top_p":.95, "top_k":20}, "http://127.0.0.1:8081/v1", "x" * 40)
    harness = online.OnlineFastHarness(dataset=load_dataset(), config=load_config(online.DEFAULT_ONLINE_CONFIG_PATH),
        dataset_path=DEFAULT_DATASET_PATH, config_path=online.DEFAULT_ONLINE_CONFIG_PATH,
        app_settings=settings, model_provider=provider)
    assert harness.provider is provider
    assert len(harness._cases_to_run) == 60
