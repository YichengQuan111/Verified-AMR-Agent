"""VeryFast 独立模型切换实验：先调吞吐与五节点，再冻结配置跑原 60 例。

只写新的实验目录，Qwen 只读取带 SHA-256 的历史报告。生产 Prompt、业务预算、
安全门禁和非流式 Provider 原样复用；速度采样与验收案例严格分开。
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import socket
import statistics
import subprocess
import sys
import time
from types import SimpleNamespace

import httpx
from openai import OpenAI
from pydantic import SecretStr

from services.config import load_settings
from services.config.settings import ModelProfileSettings
from services.model_gateway.provider import ModelProvider
from evals.p018.dataset import PROJECT_ROOT, load_config, load_dataset, DEFAULT_DATASET_PATH
from evals.p018.reproducibility import canonical_digest, sha256_file

CONFIG_PATH = PROJECT_ROOT / "config/veryfast_experiment.json"
DEFAULT_REASONING = {"enabled": True, "budget": -1}


def reasoning_of(config):
    """思考开关只来自实验配置文件；缺省沿用首轮实验的开启 + 不截断设置。"""
    return {"enabled": bool(config.get("reasoning_enabled", True)), "budget": int(config.get("reasoning_budget_tokens", -1))}


@contextmanager
def entry_budgets(config):
    """仅在本进程内放宽 PEVR 入口累计预算；生产 SHARED_ENTRY_BUDGETS 与旁路子进程不受影响。

    入口预算会写进 understand_goal 的 fixed_execution_defaults，再由模型回写到合同，
    所以放宽后每例合同预算随之变化；这是实验条件的一部分，记录在 reproducibility 里。
    """
    from agent.runtime.graph import PEVRGraphRunner
    from agent.runtime.prefix import SHARED_ENTRY_BUDGETS
    overrides = {k: int(v) for k, v in (config.get("entry_budget_overrides") or {}).items()}
    unknown = set(overrides) - set(SHARED_ENTRY_BUDGETS)
    if unknown:
        raise ValueError(f"未知入口预算字段: {sorted(unknown)}")
    original = PEVRGraphRunner.ENTRY_BUDGETS
    PEVRGraphRunner.ENTRY_BUDGETS = {**SHARED_ENTRY_BUDGETS, **overrides}
    try:
        yield {"overrides": overrides, "effective": dict(PEVRGraphRunner.ENTRY_BUDGETS)}
    finally:
        PEVRGraphRunner.ENTRY_BUDGETS = original


def write_json(path, value):
    """所有实验制品统一 UTF-8；每个阶段写独立文件，禁止覆盖历史分数。"""
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def baseline(config):
    """旧模型只允许读取冻结的 60 例在线报告，损坏或选错文件立即停止。"""
    path = PROJECT_ROOT / config["baseline_report"]
    if sha256_file(path).lower() != config["baseline_sha256"].lower():
        raise ValueError("Qwen 历史报告 SHA-256 不匹配")
    data = read_json(path)
    if len(data["cases"]) != 60 or data["reproducibility"]["model"]["alias"] != "qwen3.6-fast":
        raise ValueError("历史基线必须是 Qwen 在线完整 60 例")
    return data


def gpu_snapshot():
    """只读采样，不更改功耗、时钟或系统驱动；不可用时保留原因。"""
    result = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,memory.used,utilization.gpu,temperature.gpu,power.draw", "--format=csv,noheader,nounits"], capture_output=True, text=True)
    return {"raw": result.stdout.strip(), "returncode": result.returncode}


def server_argv(server, model, candidate, reasoning=None):
    """固定回环、单槽、全 GPU；不沿用 Qwen MoE 的 CPU 专家卸载参数。"""
    reasoning = reasoning or DEFAULT_REASONING
    return [str(server), "--model", str(model), "--alias", "VeryFast", "--host", "127.0.0.1", "--port", "18081",
            "--ctx-size", "16384", "--parallel", "1", "--gpu-layers", "all", "--fit", "off",
            "--flash-attn", "on", "--cache-type-k", candidate["kv"], "--cache-type-v", candidate["kv"],
            "--batch-size", str(candidate["batch"]), "--ubatch-size", str(candidate["ubatch"]),
            "--threads", str(candidate["threads"]), "--threads-batch", str(candidate["threads_batch"]),
            "--reasoning", "on" if reasoning["enabled"] else "off", "--reasoning-format", "deepseek",
            *(["--reasoning-budget", str(reasoning["budget"])] if reasoning["enabled"] else []),
            "--no-webui", "--no-agent", "--no-ui-mcp-proxy", "--no-mmproj", "--metrics",
            "--cors-origins", "localhost", "--no-cors-credentials"]


def port_free(port):
    with socket.socket() as sock:
        if sock.connect_ex(("127.0.0.1", port)) == 0:
            raise RuntimeError(f"实验端口 {port} 已占用；不终止未知进程")


@contextmanager
def serve(server, model, candidate, directory, key, *, proxy=False, reasoning=None):
    """隐藏子进程并按句柄回收；失败时保留日志，不按名称清理其他模型。"""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    port_free(18081)
    if proxy:
        port_free(8081)
    env = dict(os.environ, LLAMA_API_KEY=key, FAST_MODEL_API_KEY=key, FAST_MODEL_BACKEND_URL="http://127.0.0.1:18081")
    argv = server_argv(server, model, candidate, reasoning)
    write_json(directory / "launch.json", {"argv": argv, "gpu_before": gpu_snapshot()})
    processes = []
    handles = []
    try:
        for name, command, port in [("server", argv, 18081)] + ([("proxy", [sys.executable, "-m", "evals.perf.veryfast", "proxy"], 8081)] if proxy else []):
            out = (directory / f"{name}.out.log").open("w", encoding="utf-8")
            err = (directory / f"{name}.err.log").open("w", encoding="utf-8")
            handles.extend([out, err])
            process = subprocess.Popen(command, cwd=PROJECT_ROOT, env=env, stdout=out, stderr=err,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            processes.append(process)
            print(f"[VeryFast] {name} pid={process.pid} port={port}", flush=True)
            deadline = time.monotonic() + 120
            with httpx.Client(timeout=2, trust_env=False) as client:
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError(f"{name} 退出 {process.returncode}，见 {directory}")
                    try:
                        r = client.get(f"http://127.0.0.1:{port}/health", headers={"Authorization": f"Bearer {key}"})
                        if r.status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.5)
                else:
                    raise TimeoutError(f"{name} readiness timeout")
        with httpx.Client(timeout=10, trust_env=False) as client:
            props = client.get("http://127.0.0.1:18081/props", headers={"Authorization": f"Bearer {key}"}).json()
        write_json(directory / "props.json", props)
        if props["total_slots"] != 1 or props["default_generation_settings"]["n_ctx"] != 16384:
            raise ValueError("服务未按固定 16K/单槽启动")
        yield "http://127.0.0.1:8081/v1" if proxy else "http://127.0.0.1:18081/v1"
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        for handle in handles:
            handle.close()
        write_json(directory / "shutdown.json", {"pids": [p.pid for p in processes], "exit_codes": [p.returncode for p in processes], "gpu_after": gpu_snapshot()})


def output_caps(config):
    """输出侧上限来自实验配置；缺省即 Qwen 基线的 4096 单次 / PEVRRequest 默认申请值。"""
    return {"gateway_max_output_tokens": int(config.get("max_output_tokens", 4096)),
            "requested_output_tokens": config.get("requested_output_tokens")}


def make_settings(sampling, url, key, reasoning=None, max_output_tokens=4096):
    """创建仅本实验可用的 VeryFast 配置，保留同一 RAG/安全和 120s 上限；输出上限由实验配置给出。"""
    reasoning = reasoning or DEFAULT_REASONING
    settings = load_settings()
    gateway = settings.model_gateway
    gateway.profiles["veryfast"] = ModelProfileSettings(alias="VeryFast", context_window=16384,
        temperature=sampling["temperature"], top_p=sampling["top_p"], top_k=sampling["top_k"],
        parallel_slots=1, quantization="Q4_K_M", reasoning_enabled=reasoning["enabled"], reasoning_budget_tokens=reasoning["budget"])
    gateway.profile = "veryfast"
    gateway.expected_alias_override = None
    gateway.base_url = url
    gateway.api_key = SecretStr(key)
    # Fast 制品验证器只允许 IQ4_NL；实验使用下方完整哈希清单验证，不能冒充 Fast。
    gateway.artifact_verification_required = False
    gateway.prompt_cache_enabled = True
    gateway.max_output_tokens = int(max_output_tokens)
    gateway.generation_timeout_seconds = 120
    return settings


class ObservedCompletions:
    """包裹 SDK 而非重写 Provider：失败、修复、usage 与非流式行为均保留。"""
    def __init__(self, delegate, path):
        self.delegate, self.path = delegate, Path(path)
        self.case_id = None
        self.rows = []

    def create(self, **kwargs):
        started = time.perf_counter()
        row = {"case_id": self.case_id, "sequence": len(self.rows) + 1,
               "messages_sha256": canonical_digest(kwargs["messages"]), "max_tokens": kwargs["max_tokens"],
               "stream": kwargs["stream"], "ttft_ms": None, "ttft_status": "non_streaming_response"}
        try:
            result = self.delegate.create(**kwargs)
            data = result.model_dump()
            choice = data.get("choices", [{}])[0]
            message = choice.get("message", {})
            row.update(usage=data.get("usage"), timings=data.get("timings"), finish_reason=choice.get("finish_reason"),
                       content=message.get("content"), reasoning_chars=len(message.get("reasoning_content") or ""),
                       response_id=data.get("id"), status="response")
            return result
        except Exception as exc:
            # 仅保存异常类型；SDK 异常文本可能含 HTTP header，不将凭据写到制品。
            row.update(status="error", error_type=type(exc).__name__)
            raise
        finally:
            row["wall_ms"] = (time.perf_counter() - started) * 1000
            self.rows.append(row)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")


class VeryFastProvider(ModelProvider):
    """身份与采样只留在评测层，业务仍调用原 generate_structured 的一次修复链。"""
    def __init__(self, settings, path, manifest):
        client = OpenAI(base_url=settings.base_url, api_key=settings.api_key.get_secret_value(), max_retries=0,
                        http_client=httpx.Client(trust_env=False))
        self.observer = ObservedCompletions(client.chat.completions, path)
        self.manifest = manifest
        super().__init__(settings, SimpleNamespace(models=client.models, chat=SimpleNamespace(completions=self.observer)))
        self.real_client = client

    def set_case_context(self, case_id):
        self.observer.case_id = case_id

    def startup(self):
        version = super().startup()
        if version.model_sha256 is None:
            model = self.manifest["model"]
            runtime = self.manifest["runtime_binary"]
            version = version.model_copy(update={"artifact_id": "veryfast-" + model["sha256"][:16],
                "model_path": model["path"], "model_size_bytes": model["size_bytes"], "model_sha256": model["sha256"],
                "runtime_binary_path": runtime["path"], "runtime_binary_sha256": runtime["sha256"],
                "quantization": "Q4_K_M", "context_window": 16384, "temperature": self.settings.active_profile.temperature,
                "top_p": self.settings.active_profile.top_p, "top_k": self.settings.active_profile.top_k,
                "parallel_slots": 1, "reasoning_enabled": self.settings.active_profile.reasoning_enabled})
            self._version_record = version
        return version


def artifact_manifest(model, server):
    """同时固定 launcher 和推理 DLL；仅哈希小 launcher 不能代表新版 llama.cpp。"""
    def fingerprint(path):
        return {"path": str(path), "size_bytes": path.stat().st_size, "sha256": sha256_file(path)}
    return {"model": fingerprint(model), "runtime_binary": fingerprint(server),
            "runtime_libraries": [fingerprint(p) for p in sorted(server.parent.glob("*.dll"))],
            "launch_script": fingerprint(Path(__file__).resolve()),
            "runtime_version": subprocess.run([str(server), "--version"], capture_output=True, text=True).stdout.strip(),
            "sha256_verified_at": datetime.now(timezone.utc).isoformat()}


def throughput_probe(url, key, *, repeats=3, reasoning=None):
    """独立合成 6K 左右输入，固定生成 256 token；不拿 PEVR 验收题调参数。"""
    records = [{"id": f"CAL-{i:03d}", "pickup": f"P{i % 6 + 1}", "dropoff": f"S{i % 6 + 1}", "release": i, "deadline": i + 120} for i in range(100)]
    messages = [{"role": "system", "content": "你是仓储数据校验助手。以下是合成的性能校准数据，不是实际订单。\n" + json.dumps(records, ensure_ascii=False)},
                {"role": "user", "content": "检查每条记录的 deadline 是否晚于 release，逐步计算并说明。"}]
    reasoning = reasoning or DEFAULT_REASONING
    rows = []
    with httpx.Client(timeout=120, trust_env=False) as client:
        for i in range(repeats + 1):
            started = time.perf_counter()
            r = client.post(url + "/chat/completions", headers={"Authorization": f"Bearer {key}"}, json={
                "model": "VeryFast", "messages": messages, "max_tokens": 256, "temperature": 0,
                "top_p": 0.95, "top_k": 20, "seed": 20260907, "cache_prompt": False,
                "ignore_eos": True, "chat_template_kwargs": {"enable_thinking": reasoning["enabled"]},
                **({"reasoning_budget": reasoning["budget"]} if reasoning["enabled"] else {})})
            r.raise_for_status()
            data = r.json()
            rows.append({"warmup": i == 0, "wall_ms": (time.perf_counter() - started) * 1000,
                         "usage": data.get("usage"), "timings": data.get("timings"), "gpu": gpu_snapshot()})
    measured = rows[1:]
    prompt_ms = statistics.median(r["timings"]["prompt_ms"] for r in measured)
    decode = statistics.median(r["timings"]["predicted_per_second"] for r in measured)
    return {"rows": rows, "prefill_ms_p50": prompt_ms, "decode_tps_p50": decode,
            "projected_4096_ms": prompt_ms + 4096 / decode * 1000}


def calibration(settings, manifest, directory):
    """复用五节点独立虚构样例，不使用正式 60 例反馈选择参数。"""
    from scripts.smoke_p005_prompts import _build_cases
    provider = VeryFastProvider(settings.model_gateway, directory / "requests.jsonl", manifest)
    provider.startup()
    results = []
    for node, runner, context, check in _build_cases(datetime(2026, 9, 7, tzinfo=timezone.utc)):
        # 原 smoke 的 1024 是小样例局部上限；调参校准按正式 PEVR 的 4096 上限执行。
        context = context.model_copy(update={"requested_output_tokens": 4096})
        provider.set_case_context("calibration-" + node.value)
        started = time.perf_counter()
        row = {"node": node.value}
        try:
            result = runner(provider, context)
            row.update(route=result.route.value, reason_code=result.reason_code)
            if result.output is None:
                raise ValueError(result.reason_code)
            check(result.output)
            row["passed"] = result.route.value == "success"
        except Exception as exc:
            row.update(passed=False, error=f"{type(exc).__name__}: {exc}")
        row["wall_ms"] = (time.perf_counter() - started) * 1000
        results.append(row)
        print(f"[VeryFast] calibration {row}", flush=True)
        write_json(directory / "calibration.json", results)
    provider.real_client.close()
    return results


def choose_candidate(results):
    """先选速度，再在 5% 噪声带内优先无 KV 量化配置；绝不根据 60 例分数调参。"""
    valid = [r for r in results if "error" not in r]
    if not valid:
        raise ValueError("无可用吞吐配置")
    best = min(valid, key=lambda r: r["benchmark"]["projected_4096_ms"])
    near = [r for r in valid if r["candidate"]["kv"] == "f16" and r["benchmark"]["projected_4096_ms"] <= best["benchmark"]["projected_4096_ms"] * 1.05]
    return min(near, key=lambda r: r["benchmark"]["projected_4096_ms"]) if near else best


def tune(args, config, manifest):
    reasoning = reasoning_of(config)
    directory = args.output / "tuning"
    directory.mkdir(exist_ok=False)
    results = []
    key = secrets.token_urlsafe(36)
    for candidate in config["candidate_settings"]:
        row = {"candidate": candidate}
        try:
            with serve(args.server, args.model, candidate, directory / candidate["id"], key, reasoning=reasoning) as url:
                row["benchmark"] = throughput_probe(url, key, reasoning=reasoning)
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
        results.append(row)
        write_json(directory / "throughput.json", results)
        print(f"[VeryFast] tuning {candidate['id']}: {row.get('error') or row['benchmark']['projected_4096_ms']}", flush=True)
    chosen = choose_candidate(results)
    sampling_results = []
    with serve(args.server, args.model, chosen["candidate"], directory / "sampling", key, reasoning=reasoning) as url:
        for sampling in config["sampling_candidates"]:
            out = directory / sampling["id"]
            out.mkdir()
            rows = calibration(make_settings(sampling, url, key, reasoning, output_caps(config)["gateway_max_output_tokens"]), manifest, out)
            sampling_results.append({"sampling": sampling, "passed": sum(r["passed"] for r in rows), "wall_ms": sum(r["wall_ms"] for r in rows)})
    # 同分优先沿用 Qwen 采样，减少混杂；预检错误如实保留，不要求模型质量必须满分才评测。
    sampling = sorted(sampling_results, key=lambda r: (-r["passed"], r["sampling"]["id"] != "qwen_sampling"))[0]
    selection = {"candidate": chosen["candidate"], "sampling": sampling["sampling"], "sampling_results": sampling_results,
                 "reasoning_enabled": reasoning["enabled"], "reasoning_budget": reasoning["budget"], "context_window": 16384,
                 "manifest_sha256": sha256_file(args.output / "artifact_manifest.json"), "config_sha256": sha256_file(args.config)}
    write_json(args.output / "selected.json", selection)
    print("[VeryFast] selected " + json.dumps(selection, ensure_ascii=False), flush=True)


def compare_inputs(old, current):
    """上下文/案例指纹逐项核对；模型与 runtime 差异显式列出，不伪装随机同期对照。"""
    a, b = old["reproducibility"], current
    checks = {f"input_{name}": a["input_files"][name]["sha256"] == b["input_files"][name]["sha256"] for name in ("dataset", "map", "amrs", "orders")}
    checks.update(case_seeds=a["case_seed_digest"] == b["case_seed_digest"], prompt_files=a["prompt_files"] == b["prompt_files"],
                  prompt_versions=a["prompt_versions"] == b["prompt_versions"], tool_versions=a["tool_spec_versions"] == b["tool_spec_versions"],
                  context_window=a["model"]["context_window"] == b["model"]["context_window"])
    if not all(checks.values()):
        raise ValueError(f"历史基线固定输入不一致: {checks}")
    return checks


def frozen_selection(args, config):
    """probe1 与正式 60 例共用同一份冻结选择；制品或配置变动立即停止。"""
    selection = read_json(args.output / "selected.json")
    if selection["manifest_sha256"] != sha256_file(args.output / "artifact_manifest.json") or selection["config_sha256"] != sha256_file(args.config):
        raise ValueError("调参后制品或实验配置已变化")
    return selection


def probe_one(args, config, manifest):
    """关思考后先用单例试探预算：只看是否 length 截断/空响应，不计入 60 例分数。"""
    from evals.p018.online import OnlineFastHarness
    selection = frozen_selection(args, config)
    reasoning = reasoning_of(config)
    case_id = config.get("probe_case_id", "p018-normal-001")
    out = args.output / "probe1"
    out.mkdir(exist_ok=False)
    online_config = deepcopy(load_config(PROJECT_ROOT / "evals/p018/online_config.json"))
    online_config["eval_config_id"] = config["experiment_id"] + "-probe1"
    online_config["model"].update(profile="veryfast", alias="VeryFast", family="Spark-X2.5", quantization="Q4_K_M",
        reasoning_enabled=reasoning["enabled"], reasoning_budget_tokens=reasoning["budget"],
        **{k: v for k, v in selection["sampling"].items() if k != "id"})
    config_path = out / "online_config.json"
    write_json(config_path, online_config)
    key = secrets.token_urlsafe(36)
    caps = output_caps(config)
    with serve(args.server, args.model, selection["candidate"], out / "service", key, proxy=True, reasoning=reasoning) as url, entry_budgets(config) as budgets:
        settings = make_settings(selection["sampling"], url, key, reasoning, caps["gateway_max_output_tokens"])
        provider = VeryFastProvider(settings.model_gateway, out / "requests.jsonl", manifest)
        harness = OnlineFastHarness(dataset=load_dataset(), config=online_config, dataset_path=DEFAULT_DATASET_PATH,
            config_path=config_path, verification_timeout_seconds=120, app_settings=settings, model_provider=provider,
            requested_output_tokens=caps["requested_output_tokens"])
        # 评测层单例试探：只缩小执行列表，数据集指纹与 Prompt 不变。
        harness._cases_to_run = [c for c in harness._cases_to_run if c.case_id == case_id]
        if len(harness._cases_to_run) != 1:
            raise ValueError(f"probe case {case_id} 不存在")
        harness.run(output_dir=out)
        provider.real_client.close()
    rows = provider.observer.rows
    result = json.loads((out / "p018_online_progress.jsonl").read_text(encoding="utf-8").splitlines()[0])
    verdict = {"case_id": case_id, "evaluation_passed": result["evaluation_passed"], "observed_outcome": result["observed_outcome"],
               "failure_reason": (result.get("failure_reason") or "")[:200], "entry_budgets": budgets, "output_caps": caps,
               "reasoning": reasoning,
               "failure_code": result.get("failure_code"), "model_calls": len(rows),
               "finish_reasons": [r.get("finish_reason") for r in rows],
               "completion_tokens": [(r.get("usage") or {}).get("completion_tokens") for r in rows],
               "reasoning_chars": [r.get("reasoning_chars") for r in rows],
               "wall_ms": result["metrics"].get("wall_clock_ms"),
               "budget_exhausted": any(r.get("finish_reason") == "length" or r.get("status") == "error" for r in rows)
                                   or result.get("failure_code") in {"MODEL_EMPTY_RESPONSE", "TOOL_BUDGET_EXHAUSTED", "OUTPUT_BUDGET_EXHAUSTED"}}
    write_json(out / "verdict.json", verdict)
    print("[VeryFast] probe1 " + json.dumps(verdict, ensure_ascii=False), flush=True)


def run_pevr(args, config, manifest):
    """冻结选择后只执行一次完整在线 60 例，不按得分重跑或替换失败案例。"""
    from evals.p018.online import OnlineFastHarness
    from evals.p018.reporting import write_report
    selection = frozen_selection(args, config)
    reasoning = reasoning_of(config)
    old = baseline(config)
    out = args.output / "pevr60"
    out.mkdir(exist_ok=False)
    online_config = deepcopy(load_config(PROJECT_ROOT / "evals/p018/online_config.json"))
    online_config["eval_config_id"] = config["experiment_id"]
    online_config["model"].update(profile="veryfast", alias="VeryFast", family="Spark-X2.5", quantization="Q4_K_M",
        reasoning_enabled=reasoning["enabled"], reasoning_budget_tokens=reasoning["budget"],
        artifact_ref=(args.output / "artifact_manifest.json").relative_to(PROJECT_ROOT).as_posix(),
        artifact_manifest_sha256=sha256_file(args.output / "artifact_manifest.json"), model_sha256=manifest["model"]["sha256"],
        runtime_binary_sha256=manifest["runtime_binary"]["sha256"], launch_script_sha256=manifest["launch_script"]["sha256"],
        **{k:v for k,v in selection["sampling"].items() if k != "id"})
    config_path = out / "online_config.json"
    write_json(config_path, online_config)
    key = secrets.token_urlsafe(36)
    caps = output_caps(config)
    with serve(args.server, args.model, selection["candidate"], out / "service", key, proxy=True, reasoning=reasoning) as url, entry_budgets(config) as budgets:
        settings = make_settings(selection["sampling"], url, key, reasoning, caps["gateway_max_output_tokens"])
        provider = VeryFastProvider(settings.model_gateway, out / "requests.jsonl", manifest)
        harness = OnlineFastHarness(dataset=load_dataset(), config=online_config, dataset_path=DEFAULT_DATASET_PATH,
            config_path=config_path, verification_timeout_seconds=120, app_settings=settings, model_provider=provider,
            requested_output_tokens=caps["requested_output_tokens"])
        checks = compare_inputs(old, harness.reproducibility)
        harness.reproducibility["model_switch_experiment"] = {"historical_baseline": config["baseline_report"],
            "baseline_sha256": config["baseline_sha256"], "same_inputs": checks, "official_p018_publish": False,
            "reasoning_enabled": reasoning["enabled"], "reasoning_budget": reasoning["budget"],
            "entry_budgets": budgets, "output_caps": caps, "sampling": selection["sampling"], "server_settings": selection["candidate"],
            "caveats": ["历史对照，非同期随机实验", "当前 STL gate 为 p1-1.v1，历史无 STL", "模型模板/tokenizer/运行时不同",
                        "思考计入相同输出预算" if reasoning["enabled"] else "思考已关闭，与 Qwen 基线 --reasoning off 一致",
                        *([f"入口累计预算已放宽: {budgets['overrides']}（Qwen 基线为 SHARED_ENTRY_BUDGETS）"] if budgets["overrides"] else []),
                        *([f"输出上限已改: {caps}（Qwen 基线为网关 4096 / 申请 4096）"] if caps != {"gateway_max_output_tokens": 4096, "requested_output_tokens": None} else [])]}
        report = harness.run(output_dir=out)
        write_report(report, output_dir=out, json_name="p018_online_eval.json", markdown_name="p018_online_eval.md")
        provider.real_client.close()
    summarize(args, config)


def stats(values):
    """统一线性插值百分位；空集合保留 null，不用 0 假装观测。"""
    values = sorted(values)
    def percentile(p):
        x = (len(values) - 1) * p
        lo = int(x)
        return values[lo] + (values[min(lo + 1, len(values) - 1)] - values[lo]) * (x - lo)
    return {"n": len(values), "sum": sum(values), "mean": statistics.mean(values) if values else None,
            "p50": percentile(.5) if values else None, "p95": percentile(.95) if values else None}


def summarize(args, config):
    """全 60 与固定 LLM 子集分开；延迟同时给共同成功配对，避免快速失败刷速度。"""
    from evals.perf.llm36 import LLM_CASE_IDS
    old = baseline(config)
    new = read_json(args.output / "pevr60/p018_online_eval.json")
    def summary(data):
        cases = data["cases"]
        return {"report_id": data["report_id"], "pass": sum(c["evaluation_passed"] for c in cases),
            "case_count": len(cases), "metrics": data["metrics"],
            "wall_ms": stats([c["metrics"]["wall_clock_ms"] for c in cases if "wall_clock_ms" in c["metrics"]]),
            "llm36_wall_ms": stats([c["metrics"]["wall_clock_ms"] for c in cases if c["case_id"] in LLM_CASE_IDS]),
            "failures": [{k:c.get(k) for k in ("case_id", "expected_outcome", "observed_outcome", "failure_code", "failure_reason")} for c in cases if not c["evaluation_passed"]]}
    old_cases = {c["case_id"]:c for c in old["cases"]}
    paired = [c for c in new["cases"] if c["case_id"] in LLM_CASE_IDS and c["evaluation_passed"] and old_cases[c["case_id"]]["evaluation_passed"]]
    payload = {"baseline": summary(old), "veryfast": summary(new), "paired_llm_success_count": len(paired),
        "paired_old_ms": stats([old_cases[c["case_id"]]["metrics"]["wall_clock_ms"] for c in paired]),
        "paired_new_ms": stats([c["metrics"]["wall_clock_ms"] for c in paired]),
        "baseline_sha256": config["baseline_sha256"], "veryfast_sha256": sha256_file(args.output / "pevr60/p018_online_eval.json"),
        "ttft_ms": None, "ttft_status": "non_streaming_response", "historical_control": True}
    write_json(args.output / "comparison.json", payload)
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["tune", "probe1", "run", "summarize", "proxy"])
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--model", type=Path, default=os.environ.get("VERYFAST_MODEL_PATH"))
    parser.add_argument("--server", type=Path, default=os.environ.get("LLAMA_SERVER_PATH"))
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "tmp/veryfast_20260907")
    args = parser.parse_args()
    if args.stage == "proxy":
        import uvicorn
        from services.model_gateway.secure_proxy import create_proxy_app
        uvicorn.run(create_proxy_app(api_key=os.environ["FAST_MODEL_API_KEY"], backend_url="http://127.0.0.1:18081"), host="127.0.0.1", port=8081, log_level="warning")
        return
    args.output = args.output.resolve()
    args.output.relative_to(PROJECT_ROOT)
    args.config = args.config.resolve()
    config = read_json(args.config)
    baseline(config)
    args.output.mkdir(parents=True, exist_ok=True)
    if args.stage == "summarize":
        summarize(args, config)
        return
    if not args.model or not args.server:
        parser.error("须提供 --model/--server 或 VERYFAST_MODEL_PATH/LLAMA_SERVER_PATH")
    args.model, args.server = args.model.resolve(), args.server.resolve()
    if args.stage == "tune":
        manifest = artifact_manifest(args.model, args.server)
        if (args.output / "artifact_manifest.json").exists():
            raise ValueError("实验目录已有 manifest，请使用新输出目录")
        write_json(args.output / "artifact_manifest.json", manifest)
        tune(args, config, manifest)
    else:
        manifest = read_json(args.output / "artifact_manifest.json")
        for item in [manifest["model"], manifest["runtime_binary"], *manifest["runtime_libraries"]]:
            if sha256_file(Path(item["path"])) != item["sha256"]:
                raise ValueError("调参后的模型/运行时字节已变化")
        if str(args.model) != manifest["model"]["path"] or str(args.server) != manifest["runtime_binary"]["path"]:
            raise ValueError("运行路径必须等于调参路径")
        (probe_one if args.stage == "probe1" else run_pevr)(args, config, manifest)


if __name__ == "__main__":
    main()

