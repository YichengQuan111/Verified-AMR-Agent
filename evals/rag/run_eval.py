"""运行 20 例 P0-07 RAG 评测并计算排序、引用、ACL 与拒答指标。

阈值只允许从 calibration 子集建议；对外发布的 Recall/MRR/citation/answerability/ACL
只在未参与调参的 test+attack 上计算。CLI 在任一发布指标失败或 citation_total=0 时
必须非零退出，不能只按 ACL 假绿。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

from agent.tools import UserRole
from services.application import DocumentService
from services.config import load_settings
from services.persistence import create_database_runtime
from services.retrieval import (
    BM25Index,
    Embedder,
    HybridRetriever,
    QdrantVectorStore,
    RetrievalStatus,
    WarehouseKnowledgeIndexer,
    load_knowledge_chunks,
)


DEFAULT_CASES = Path(__file__).with_name("cases.json")
DEFAULT_KNOWLEDGE_ROOT = (
    PROJECT_ROOT / "domains" / "amr_warehouse" / "knowledge"
)
DEFAULT_METRIC_GATES = {
    "recall_at_k": 0.8,
    "mrr": 0.8,
    "citation_correctness": 0.9,
    "answerability_accuracy": 0.8,
}
REQUIRED_SPLITS = ("calibration", "test", "attack")


class EvalContract(BaseModel):
    """评测数据同样拒绝额外字段，避免指标因拼写错误失真。"""

    model_config = ConfigDict(extra="forbid", validate_default=True)


class ExpectedCitation(EvalContract):
    """语义相关性的文档与 section 金标准。"""

    doc_id: str = Field(min_length=1)
    section: str = Field(min_length=1)


class RAGEvalCase(EvalContract):
    """一条带角色、可答性、金标准和禁止文档的评测问题。"""

    case_id: str = Field(min_length=1)
    category: str = Field(min_length=1)
    query: str = Field(min_length=1)
    role_scope: UserRole
    answerable: bool
    top_k: int = Field(ge=1, le=20)
    expected_citations: list[ExpectedCitation]
    forbidden_doc_ids: list[str]

    @model_validator(mode="after")
    def validate_expectations(self) -> "RAGEvalCase":
        """可答问题必须有金标准，不可答问题不能伪造期望来源。"""

        if self.answerable and not self.expected_citations:
            raise ValueError("answerable case 必须提供 expected_citations")
        if not self.answerable and self.expected_citations:
            raise ValueError("unanswerable case 不能提供 expected_citations")
        identities = [
            (citation.doc_id, citation.section)
            for citation in self.expected_citations
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("expected_citations 不能重复")
        if len(self.forbidden_doc_ids) != len(set(self.forbidden_doc_ids)):
            raise ValueError("forbidden_doc_ids 不能重复")
        return self


def _parse_cases(payload: Any) -> list[RAGEvalCase]:
    """把 JSON 数组校验成评测用例。"""

    if not isinstance(payload, list):
        raise ValueError("RAG eval cases 必须是数组")
    cases = [RAGEvalCase.model_validate(item) for item in payload]
    if len(cases) < 20:
        raise ValueError(f"RAG eval 至少需要 20 例，当前 {len(cases)}")
    case_ids = [case.case_id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("RAG eval case_id 不能重复")
    return cases


def _load_cases(path: Path) -> list[RAGEvalCase]:
    """读取固定 JSON；兼容历史顶层数组和带 split 的对象。"""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return _parse_cases(payload)
    if not isinstance(payload, dict):
        raise ValueError("RAG eval 文件顶层必须是数组或带 splits 的对象")
    return _parse_cases(payload.get("cases"))


def load_eval_bundle(path: Path) -> tuple[list[RAGEvalCase], dict[str, list[str]]]:
    """加载评测包，并强制 calibration/test/attack 互斥且覆盖全部 case。"""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        cases = _parse_cases(payload)
        splits = {"calibration": [], "test": [case.case_id for case in cases], "attack": []}
        return cases, splits
    if not isinstance(payload, dict):
        raise ValueError("RAG eval 文件顶层必须是数组或对象")
    if payload.get("purpose") != "evaluation_only" or payload.get("is_training_data") is not False:
        raise ValueError("RAG eval 必须声明 evaluation_only 且 is_training_data=false")
    cases = _parse_cases(payload.get("cases"))
    raw_splits = payload.get("splits")
    if not isinstance(raw_splits, dict):
        raise ValueError("RAG eval 对象必须提供 splits")
    splits: dict[str, list[str]] = {}
    seen: list[str] = []
    known = {case.case_id for case in cases}
    for name in REQUIRED_SPLITS:
        ids = raw_splits.get(name)
        if not isinstance(ids, list) or not ids:
            raise ValueError(f"RAG eval splits.{name} 必须是非空数组")
        values = [str(item) for item in ids]
        if len(values) != len(set(values)):
            raise ValueError(f"RAG eval splits.{name} 不能重复")
        unknown = sorted(set(values) - known)
        if unknown:
            raise ValueError(f"RAG eval splits.{name} 引用了未知 case: {', '.join(unknown)}")
        overlap = sorted(set(values) & set(seen))
        if overlap:
            raise ValueError(f"RAG eval split 不能交叉: {', '.join(overlap)}")
        splits[name] = values
        seen.extend(values)
    missing = sorted(known - set(seen))
    if missing:
        raise ValueError(f"RAG eval 仍有未划分 case: {', '.join(missing)}")
    return cases, splits


def _score_distribution(values: list[float]) -> dict[str, Any]:
    """输出原始样本与汇总，便于阈值校准而不是只看平均数。"""

    if not values:
        return {"count": 0, "values": [], "min": None, "max": None, "mean": None}
    return {
        "count": len(values),
        "values": [round(value, 6) for value in values],
        "min": round(min(values), 6),
        "max": round(max(values), 6),
        "mean": round(statistics.fmean(values), 6),
    }


def evaluate_metric_gates(
    report: dict[str, Any],
    *,
    gates: dict[str, float] | None = None,
) -> list[str]:
    """发布门禁：ACL、citation 分母和 Recall/MRR/引用/拒答都必须达标。"""

    metrics = report.get("metrics")
    if not isinstance(metrics, dict):
        return ["metrics_missing"]
    failures: list[str] = []
    if int(metrics.get("acl_leak_count") or 0) != 0:
        failures.append("acl_leak_count")
    if int(metrics.get("citation_total") or 0) <= 0:
        failures.append("citation_total")
    selected = gates or DEFAULT_METRIC_GATES
    for name, minimum in selected.items():
        observed = float(metrics.get(name) or 0.0)
        if observed < float(minimum):
            failures.append(name)
    return failures


def run_evaluation(
    *,
    cases: list[RAGEvalCase],
    retriever: HybridRetriever,
    chunks_by_id: dict[str, Any],
    metric_case_ids: set[str] | None = None,
    calibration_case_ids: set[str] | None = None,
) -> dict[str, Any]:
    """执行全部查询；发布指标与阈值建议使用互斥集合。"""

    published_ids = metric_case_ids or {case.case_id for case in cases}
    calibration_ids = calibration_case_ids or set()
    recall_values: list[float] = []
    reciprocal_ranks: list[float] = []
    section_recall_values: list[float] = []
    answerable_scores: list[float] = []
    unanswerable_scores: list[float] = []
    answerable_vector_scores: list[float] = []
    unanswerable_vector_scores: list[float] = []
    fallback_answerable_vectors: list[float] = []
    fallback_unanswerable_vectors: list[float] = []
    citation_total = 0
    citation_correct = 0
    acl_leaks: list[dict[str, str]] = []
    answerability_correct = 0
    published_count = 0
    case_reports: list[dict[str, Any]] = []

    for case in cases:
        candidates = retriever.rank_candidates(
            case.query,
            role_scope=case.role_scope,
            top_k=case.top_k,
        )
        response = retriever.apply_evidence_threshold(
            case.query,
            role_scope=case.role_scope,
            top_k=case.top_k,
            candidates=candidates,
        )
        predicted_answerable = response.status is RetrievalStatus.ANSWERABLE
        top_score = candidates[0].hybrid_score if candidates else 0.0
        top_vector_score = candidates[0].vector_score if candidates else -1.0
        publish = case.case_id in published_ids
        calibrate = case.case_id in calibration_ids
        if publish:
            published_count += 1
            answerability_correct += int(predicted_answerable == case.answerable)
        if calibrate:
            (answerable_scores if case.answerable else unanswerable_scores).append(top_score)
            (
                answerable_vector_scores
                if case.answerable
                else unanswerable_vector_scores
            ).append(top_vector_score)
            if top_score < retriever.minimum_hybrid_score:
                (
                    fallback_answerable_vectors
                    if case.answerable
                    else fallback_unanswerable_vectors
                ).append(top_vector_score)

        expected_docs = {item.doc_id for item in case.expected_citations}
        expected_sections = {
            (item.doc_id, item.section) for item in case.expected_citations
        }
        retrieved_docs = [item.doc_id for item in candidates]
        retrieved_sections = [(item.doc_id, item.section) for item in candidates]
        if publish and case.answerable:
            recall_values.append(
                len(expected_docs & set(retrieved_docs)) / len(expected_docs)
            )
            first_rank = next(
                (
                    index
                    for index, doc_id in enumerate(retrieved_docs, start=1)
                    if doc_id in expected_docs
                ),
                None,
            )
            reciprocal_ranks.append(0.0 if first_rank is None else 1.0 / first_rank)
            section_recall_values.append(
                len(expected_sections & set(retrieved_sections))
                / len(expected_sections)
            )

        if publish:
            for result in response.results:
                citation_total += 1
                source_chunk = chunks_by_id.get(result.chunk_id)
                expected_citation = (
                    f"{result.doc_id}@{result.version} § {result.section} "
                    f"[{result.chunk_id}]"
                )
                if (
                    source_chunk is not None
                    and source_chunk.doc_id == result.doc_id
                    and source_chunk.section == result.section
                    and source_chunk.version == result.version
                    and source_chunk.checksum == result.checksum
                    and source_chunk.text == result.text
                    and result.citation == expected_citation
                ):
                    citation_correct += 1
            for result in candidates:
                reasons: list[str] = []
                if case.role_scope not in result.role_scope:
                    reasons.append("role_scope_mismatch")
                if result.doc_id in case.forbidden_doc_ids:
                    reasons.append("forbidden_doc_id")
                for reason in reasons:
                    acl_leaks.append(
                        {
                            "case_id": case.case_id,
                            "chunk_id": result.chunk_id,
                            "doc_id": result.doc_id,
                            "reason": reason,
                        }
                    )

        split_name = next(
            (name for name, ids in (("calibration", calibration_ids), ("holdout", published_ids)) if case.case_id in ids),
            "unassigned",
        )
        case_reports.append(
            {
                "case_id": case.case_id,
                "category": case.category,
                "split": split_name,
                "used_for_published_metrics": publish,
                "used_for_threshold_calibration": calibrate,
                "role_scope": case.role_scope.value,
                "expected_answerable": case.answerable,
                "status": response.status.value,
                "top_score": round(top_score, 6),
                "top_vector_score": round(top_vector_score, 6),
                "retrieved": [
                    {
                        "rank": rank,
                        "doc_id": item.doc_id,
                        "section": item.section,
                        "hybrid_score": round(item.hybrid_score, 6),
                        "vector_score": round(item.vector_score, 6),
                        "bm25_score": round(item.bm25_score, 6),
                    }
                    for rank, item in enumerate(candidates, start=1)
                ],
            }
        )

    min_answerable = min(answerable_scores) if answerable_scores else None
    max_unanswerable = max(unanswerable_scores) if unanswerable_scores else None
    suggested_threshold = None
    if (
        min_answerable is not None
        and max_unanswerable is not None
        and min_answerable > max_unanswerable
    ):
        suggested_threshold = (min_answerable + max_unanswerable) / 2.0

    suggested_vector_threshold = None
    if fallback_answerable_vectors and fallback_unanswerable_vectors:
        min_fallback_answerable = min(fallback_answerable_vectors)
        max_fallback_unanswerable = max(fallback_unanswerable_vectors)
        if min_fallback_answerable > max_fallback_unanswerable:
            suggested_vector_threshold = (
                min_fallback_answerable + max_fallback_unanswerable
            ) / 2.0

    return {
        "case_count": len(cases),
        "published_case_count": published_count,
        "metrics": {
            "recall_at_k": statistics.fmean(recall_values) if recall_values else 0.0,
            "mrr": statistics.fmean(reciprocal_ranks) if reciprocal_ranks else 0.0,
            "section_recall_at_k": (
                statistics.fmean(section_recall_values) if section_recall_values else 0.0
            ),
            "citation_correctness": (
                citation_correct / citation_total if citation_total else 0.0
            ),
            "citation_correct": citation_correct,
            "citation_total": citation_total,
            "acl_leak_count": len(acl_leaks),
            "answerability_accuracy": (
                answerability_correct / published_count if published_count else 0.0
            ),
        },
        "threshold": {
            "source": "calibration_only",
            "configured": retriever.minimum_hybrid_score,
            "suggested_if_separable": suggested_threshold,
            "answerable_top_scores": _score_distribution(answerable_scores),
            "unanswerable_top_scores": _score_distribution(unanswerable_scores),
            "configured_vector": retriever.minimum_vector_score,
            "answerable_top_vector_scores": _score_distribution(
                answerable_vector_scores
            ),
            "unanswerable_top_vector_scores": _score_distribution(
                unanswerable_vector_scores
            ),
            "fallback_vector_suggested_if_separable": suggested_vector_threshold,
            "fallback_answerable_vector_scores": _score_distribution(
                fallback_answerable_vectors
            ),
            "fallback_unanswerable_vector_scores": _score_distribution(
                fallback_unanswerable_vectors
            ),
        },
        "acl_leaks": acl_leaks,
        "cases": case_reports,
    }


def main() -> int:
    """命令行入口；可选先重建索引，再在同一模型实例上执行评测。"""

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--knowledge-root", type=Path, default=DEFAULT_KNOWLEDGE_ROOT)
    parser.add_argument("--rebuild-index", action="store_true")
    parser.add_argument("--minimum-hybrid-score", type=float, default=None)
    parser.add_argument("--minimum-vector-score", type=float, default=None)
    parser.add_argument("--min-recall", type=float, default=DEFAULT_METRIC_GATES["recall_at_k"])
    parser.add_argument("--min-mrr", type=float, default=DEFAULT_METRIC_GATES["mrr"])
    parser.add_argument("--min-citation", type=float, default=DEFAULT_METRIC_GATES["citation_correctness"])
    parser.add_argument("--min-answerability", type=float, default=DEFAULT_METRIC_GATES["answerability_accuracy"])
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    settings = load_settings(args.config)
    if args.minimum_hybrid_score is not None:
        settings.retrieval.minimum_hybrid_score = args.minimum_hybrid_score
    if args.minimum_vector_score is not None:
        settings.retrieval.minimum_vector_score = args.minimum_vector_score
    cases, splits = load_eval_bundle(args.cases)
    published_ids = set(splits["test"]) | set(splits["attack"])
    calibration_ids = set(splits["calibration"])
    embedder = Embedder(
        settings.retrieval.embedding_model_path,
        device=settings.retrieval.embedding_device,
        batch_size=settings.retrieval.embedding_batch_size,
    )
    vector_store = QdrantVectorStore(
        url=settings.retrieval.qdrant_url,
        collection_name=settings.retrieval.collection_name,
        api_key=(
            settings.retrieval.qdrant_api_key.get_secret_value()
            if settings.retrieval.qdrant_api_key is not None
            else None
        ),
    )

    runtime = None
    try:
        index_report = None
        if args.rebuild_index:
            runtime = create_database_runtime(settings.database)
            index_report = WarehouseKnowledgeIndexer(
                settings.retrieval,
                document_service=DocumentService(runtime.session_factory),
                embedder=embedder,
                vector_store=vector_store,
            ).index_directory(args.knowledge_root, rebuild=True)
        _, chunks = load_knowledge_chunks(args.knowledge_root, settings.retrieval)
        retriever = HybridRetriever(
            embedder=embedder,
            vector_store=vector_store,
            bm25_index=BM25Index(chunks),
            vector_weight=settings.retrieval.vector_weight,
            bm25_weight=settings.retrieval.bm25_weight,
            candidate_multiplier=settings.retrieval.candidate_multiplier,
            bm25_saturation=settings.retrieval.bm25_saturation,
            minimum_hybrid_score=settings.retrieval.minimum_hybrid_score,
            minimum_vector_score=settings.retrieval.minimum_vector_score,
        )
        report = run_evaluation(
            cases=cases,
            retriever=retriever,
            chunks_by_id={chunk.chunk_id: chunk for chunk in chunks},
            metric_case_ids=published_ids,
            calibration_case_ids=calibration_ids,
        )
        report["splits"] = splits
        report["runtime"] = {
            "collection_name": settings.retrieval.collection_name,
            "collection_point_count": vector_store.count(),
            "embedding_model_path": str(embedder.model_path),
            "embedding_dimension": embedder.dimension,
            "index_report": (
                None
                if index_report is None
                else index_report.model_dump(mode="json")
            ),
        }
        gates = {
            "recall_at_k": args.min_recall,
            "mrr": args.min_mrr,
            "citation_correctness": args.min_citation,
            "answerability_accuracy": args.min_answerability,
        }
        gate_failures = evaluate_metric_gates(report, gates=gates)
        report["metric_gates"] = {
            "passed": not gate_failures,
            "failures": gate_failures,
            "thresholds": gates,
        }
        rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered + "\n", encoding="utf-8", newline="\n")
        print(rendered)
        return 0 if not gate_failures else 2
    finally:
        if runtime is not None:
            runtime.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
