"""RAG 评测 split 与 CLI 指标门禁，不依赖 Qdrant/Embedding。"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from evals.rag.run_eval import (
    DEFAULT_METRIC_GATES,
    _load_cases,
    calculate_section_rank_metrics,
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


def test_section_rank_metrics_use_fixed_k_and_ignore_duplicate_chunks() -> None:
    """同一相关章节的重复 chunk 不得抬高 Precision 或 nDCG。"""

    precision, ndcg = calculate_section_rank_metrics(
        [
            ("doc-a", "section-1"),
            ("doc-a", "section-1"),
            ("doc-b", "section-2"),
        ],
        {("doc-a", "section-1"), ("doc-b", "section-2")},
        k=5,
    )

    assert precision == pytest.approx(2 / 5)
    expected_dcg = 1.0 + 1.0 / math.log2(4)
    ideal_dcg = 1.0 + 1.0 / math.log2(3)
    assert ndcg == pytest.approx(expected_dcg / ideal_dcg)


def test_section_rank_metrics_reject_missing_oracle_and_support_optional_gates() -> None:
    """不可答 case 不伪造排序分数；显式启用的新门禁仍会 fail closed。"""

    with pytest.raises(ValueError, match="至少一个期望"):
        calculate_section_rank_metrics([], set(), k=5)
    with pytest.raises(ValueError, match="K 必须大于 0"):
        calculate_section_rank_metrics(
            [("doc-a", "section-1")],
            {("doc-a", "section-1")},
            k=0,
        )

    report = {
        "metrics": {
            "recall_at_k": 1.0,
            "mrr": 1.0,
            "precision_at_k": 0.2,
            "ndcg_at_k": 0.9,
            "citation_correctness": 1.0,
            "citation_total": 10,
            "answerability_accuracy": 1.0,
            "acl_leak_count": 0,
        }
    }
    gates = {
        **DEFAULT_METRIC_GATES,
        "precision_at_k": 0.25,
        "ndcg_at_k": 0.95,
    }
    assert evaluate_metric_gates(report, gates=gates) == [
        "precision_at_k",
        "ndcg_at_k",
    ]
