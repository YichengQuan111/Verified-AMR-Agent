"""RAG 评测 split 与 CLI 指标门禁，不依赖 Qdrant/Embedding。"""

from __future__ import annotations

from pathlib import Path

from evals.rag.run_eval import (
    DEFAULT_METRIC_GATES,
    _load_cases,
    evaluate_metric_gates,
    load_eval_bundle,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_rag_eval_bundle_has_disjoint_calibration_test_attack() -> None:
    """calibration/test/attack 必须互斥且覆盖全部 20 例。"""

    cases, splits = load_eval_bundle(PROJECT_ROOT / "evals" / "rag" / "cases.json")
    assert len(cases) == 20
    assert len(splits["calibration"]) == 8
    assert len(splits["test"]) == 8
    assert len(splits["attack"]) == 4
    assigned = splits["calibration"] + splits["test"] + splits["attack"]
    assert len(assigned) == len(set(assigned)) == 20
    assert {case.case_id for case in cases} == set(assigned)
    assert _load_cases(PROJECT_ROOT / "evals" / "rag" / "cases.json") == cases


def test_metric_gates_fail_closed_on_zero_citations_and_low_scores() -> None:
    """citation_total=0 或 Recall/MRR/引用/拒答不达标时必须失败，不能只看 ACL。"""

    healthy = {
        "metrics": {
            "recall_at_k": 1.0,
            "mrr": 0.97,
            "citation_correctness": 1.0,
            "citation_total": 40,
            "answerability_accuracy": 1.0,
            "acl_leak_count": 0,
        }
    }
    assert evaluate_metric_gates(healthy) == []
    zero_citation = {
        "metrics": {
            "recall_at_k": 1.0,
            "mrr": 1.0,
            "citation_correctness": 0.0,
            "citation_total": 0,
            "answerability_accuracy": 0.15,
            "acl_leak_count": 0,
        }
    }
    failures = evaluate_metric_gates(zero_citation)
    assert "citation_total" in failures
    assert "citation_correctness" in failures
    assert "answerability_accuracy" in failures
    assert evaluate_metric_gates(
        {
            "metrics": {
                "recall_at_k": 1.0,
                "mrr": 1.0,
                "citation_correctness": 1.0,
                "citation_total": 10,
                "answerability_accuracy": 1.0,
                "acl_leak_count": 2,
            }
        }
    ) == ["acl_leak_count"]
    assert DEFAULT_METRIC_GATES["recall_at_k"] <= 0.8
