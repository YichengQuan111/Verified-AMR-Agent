"""Vector + BM25 分数归一化融合、ACL 防御与证据不足判定。"""

from __future__ import annotations

from collections.abc import Sequence
from math import exp
from typing import Protocol

import numpy as np

from agent.tools import UserRole
from services.retrieval.bm25 import BM25Index
from services.retrieval.contracts import (
    RetrievalResponse,
    RetrievalResult,
    RetrievalStatus,
    VectorSearchHit,
)


class QueryEmbedderProtocol(Protocol):
    """HybridRetriever 所需的最小查询向量接口。"""

    def embed_query(self, query: str) -> np.ndarray: ...


class VectorSearchProtocol(Protocol):
    """允许单元测试替换 Qdrant，但生产实现仍在服务端执行 ACL。"""

    def search(
        self,
        query_vector: np.ndarray,
        *,
        role_scope: UserRole,
        limit: int,
        document_ids: Sequence[str] | None = None,
    ) -> list[VectorSearchHit]: ...


class HybridRetriever:
    """对两路候选做可解释的归一化加权，不包含 Reranker。"""

    def __init__(
        self,
        *,
        embedder: QueryEmbedderProtocol,
        vector_store: VectorSearchProtocol,
        bm25_index: BM25Index,
        vector_weight: float = 0.5,
        bm25_weight: float = 0.5,
        candidate_multiplier: int = 4,
        bm25_saturation: float = 3.0,
        minimum_hybrid_score: float = 0.0,
        minimum_vector_score: float = 0.499,
    ) -> None:
        if abs((vector_weight + bm25_weight) - 1.0) > 1e-9:
            raise ValueError("vector_weight + bm25_weight 必须等于 1.0")
        if vector_weight < 0 or bm25_weight < 0:
            raise ValueError("融合权重不能为负")
        if candidate_multiplier < 1:
            raise ValueError("candidate_multiplier 必须大于 0")
        if bm25_saturation <= 0:
            raise ValueError("bm25_saturation 必须大于 0")
        if not 0.0 <= minimum_hybrid_score <= 1.0:
            raise ValueError("minimum_hybrid_score 必须位于 0..1")
        if not -1.0 <= minimum_vector_score <= 1.0:
            raise ValueError("minimum_vector_score 必须位于 -1..1")
        self.embedder = embedder
        self.vector_store = vector_store
        self.bm25_index = bm25_index
        self.vector_weight = vector_weight
        self.bm25_weight = bm25_weight
        self.candidate_multiplier = candidate_multiplier
        self.bm25_saturation = bm25_saturation
        self.minimum_hybrid_score = minimum_hybrid_score
        self.minimum_vector_score = minimum_vector_score

    @staticmethod
    def _normalise_vector(score: float | None) -> float:
        """把 cosine ``[-1, 1]`` 映射到稳定 ``[0, 1]``，保留绝对置信度。"""

        if score is None:
            return 0.0
        clipped = min(1.0, max(-1.0, score))
        return (clipped + 1.0) / 2.0

    def _normalise_bm25(self, score: float | None) -> float:
        """用可配置饱和尺度把非负 BM25 分数压到 ``[0, 1)``。"""

        if score is None or score <= 0.0:
            return 0.0
        return 1.0 - exp(-score / self.bm25_saturation)

    def rank_candidates(
        self,
        query: str,
        *,
        role_scope: UserRole,
        top_k: int,
        document_ids: Sequence[str] | None = None,
    ) -> list[RetrievalResult]:
        """返回阈值判定前候选，供评测观察 answerable/unanswerable 分布。"""

        normalised_query = query.strip()
        if not normalised_query:
            raise ValueError("检索 query 不能为空")
        if top_k < 1:
            raise ValueError("top_k 必须大于 0")
        candidate_limit = max(top_k, top_k * self.candidate_multiplier)
        query_vector = self.embedder.embed_query(normalised_query)
        vector_hits = self.vector_store.search(
            query_vector,
            role_scope=role_scope,
            limit=candidate_limit,
            document_ids=document_ids,
        )
        bm25_hits = self.bm25_index.search(
            normalised_query,
            role_scope=role_scope,
            limit=candidate_limit,
            document_ids=document_ids,
        )

        candidates: dict[str, dict[str, object]] = {}
        for hit in vector_hits:
            candidates[hit.chunk.chunk_id] = {
                "chunk": hit.chunk,
                "vector_score": hit.score,
                "bm25_score": None,
            }
        for hit in bm25_hits:
            existing = candidates.setdefault(
                hit.chunk.chunk_id,
                {"chunk": hit.chunk, "vector_score": None, "bm25_score": None},
            )
            other_chunk = existing["chunk"]
            if getattr(other_chunk, "checksum") != hit.chunk.checksum:
                raise RuntimeError(f"两路索引的 chunk 版本不一致: {hit.chunk.chunk_id}")
            existing["bm25_score"] = hit.score

        results: list[RetrievalResult] = []
        for item in candidates.values():
            chunk = item["chunk"]
            vector_score = item["vector_score"]
            bm25_score = item["bm25_score"]
            if role_scope not in chunk.role_scope:
                # BM25 和 Qdrant 都应在候选阶段完成 ACL；这里仅作为泄漏熔断。
                raise RuntimeError(f"融合前发现越权 chunk: {chunk.chunk_id}")
            normalised_vector = self._normalise_vector(vector_score)
            normalised_bm25 = self._normalise_bm25(bm25_score)
            hybrid_score = (
                self.vector_weight * normalised_vector
                + self.bm25_weight * normalised_bm25
            )
            citation = (
                f"{chunk.doc_id}@{chunk.version} § {chunk.section} "
                f"[{chunk.chunk_id}]"
            )
            results.append(
                RetrievalResult(
                    chunk_id=chunk.chunk_id,
                    doc_id=chunk.doc_id,
                    title=chunk.title,
                    section=chunk.section,
                    version=chunk.version,
                    role_scope=chunk.role_scope,
                    source=chunk.source,
                    checksum=chunk.checksum,
                    text=chunk.text,
                    frozen_at=chunk.frozen_at,
                    citation=citation,
                    hybrid_score=hybrid_score,
                    vector_score=(
                        0.0 if vector_score is None else float(vector_score)
                    ),
                    bm25_score=0.0 if bm25_score is None else float(bm25_score),
                    normalized_vector_score=normalised_vector,
                    normalized_bm25_score=normalised_bm25,
                )
            )
        results.sort(key=lambda result: (-result.hybrid_score, result.chunk_id))
        return results[:top_k]

    def retrieve(
        self,
        query: str,
        *,
        role_scope: UserRole,
        top_k: int,
        document_ids: Sequence[str] | None = None,
    ) -> RetrievalResponse:
        """应用配置化阈值；证据不足时返回固定状态且不暴露弱正文。"""

        candidates = self.rank_candidates(
            query,
            role_scope=role_scope,
            top_k=top_k,
            document_ids=document_ids,
        )
        return self.apply_evidence_threshold(
            query,
            role_scope=role_scope,
            top_k=top_k,
            candidates=candidates,
        )

    def apply_evidence_threshold(
        self,
        query: str,
        *,
        role_scope: UserRole,
        top_k: int,
        candidates: Sequence[RetrievalResult],
    ) -> RetrievalResponse:
        """对已排序候选应用同一拒答策略，供评测避免重复计算向量。"""

        if top_k < 1:
            raise ValueError("top_k 必须大于 0")
        if any(role_scope not in item.role_scope for item in candidates):
            raise RuntimeError("阈值判定收到越权候选")
        if any(
            candidates[index].hybrid_score
            < candidates[index + 1].hybrid_score
            for index in range(len(candidates) - 1)
        ):
            raise ValueError("candidates 必须按 hybrid_score 降序排列")
        top_score = candidates[0].hybrid_score if candidates else None
        top_vector_score = candidates[0].vector_score if candidates else None
        hybrid_passed = (
            top_score is not None and top_score >= self.minimum_hybrid_score
        )
        vector_passed = (
            top_vector_score is not None
            and top_vector_score >= self.minimum_vector_score
        )
        if not hybrid_passed and not vector_passed:
            return RetrievalResponse(
                query=query.strip(),
                role_scope=role_scope,
                status=RetrievalStatus.INSUFFICIENT_EVIDENCE,
                reason="知识库没有达到配置化证据阈值的引用",
                top_k=top_k,
                minimum_hybrid_score=self.minimum_hybrid_score,
                minimum_vector_score=self.minimum_vector_score,
                top_candidate_score=top_score,
                top_candidate_vector_score=top_vector_score,
                results=[],
            )
        return RetrievalResponse(
            query=query.strip(),
            role_scope=role_scope,
            status=RetrievalStatus.ANSWERABLE,
            reason=(
                "已找到达到 hybrid 证据阈值的仓储知识引用"
                if hybrid_passed
                else "已找到达到 vector 语义证据阈值的仓储知识引用"
            ),
            top_k=top_k,
            minimum_hybrid_score=self.minimum_hybrid_score,
            minimum_vector_score=self.minimum_vector_score,
            top_candidate_score=top_score,
            top_candidate_vector_score=top_vector_score,
            results=list(candidates),
        )


__all__ = ["HybridRetriever", "QueryEmbedderProtocol", "VectorSearchProtocol"]
