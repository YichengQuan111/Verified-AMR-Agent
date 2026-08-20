"""基于 jieba.lcut 与 BM25Okapi 的进程内关键词检索。"""

from __future__ import annotations

from collections.abc import Sequence

import jieba
from rank_bm25 import BM25Okapi

from agent.tools import UserRole
from services.retrieval.contracts import BM25SearchHit, KnowledgeChunk


def tokenize_chinese(text: str) -> list[str]:
    """使用冻结的 jieba 分词入口，并丢弃纯空白 token。"""

    return [token.strip().lower() for token in jieba.lcut(text) if token.strip()]


class BM25Index:
    """保存 chunk 语料；每次查询先做 ACL/文档范围过滤再建立候选索引。

    P0 知识库规模很小，按角色即时构建 BM25 的成本可控，并能从结构上保证
    operator-only 文档根本不会进入 viewer 的 BM25 候选矩阵。
    """

    def __init__(self, chunks: Sequence[KnowledgeChunk]) -> None:
        self._chunks = list(chunks)
        self._tokens = {
            chunk.chunk_id: tokenize_chinese(chunk.text) for chunk in self._chunks
        }

    def search(
        self,
        query: str,
        *,
        role_scope: UserRole,
        limit: int,
        document_ids: Sequence[str] | None = None,
    ) -> list[BM25SearchHit]:
        """在 ACL 预过滤语料上计算 BM25，并只返回正证据分数。"""

        if not query.strip():
            raise ValueError("检索 query 不能为空")
        if limit < 1:
            raise ValueError("limit 必须大于 0")
        allowed_documents = set(document_ids) if document_ids is not None else None
        allowed_chunks = [
            chunk
            for chunk in self._chunks
            if role_scope in chunk.role_scope
            and (allowed_documents is None or chunk.doc_id in allowed_documents)
        ]
        if not allowed_chunks:
            return []
        query_tokens = tokenize_chinese(query)
        if not query_tokens:
            return []

        # 先过滤 chunks，再传给 BM25Okapi；不能在全库评分后隐藏越权结果。
        corpus = [self._tokens[chunk.chunk_id] for chunk in allowed_chunks]
        bm25 = BM25Okapi(corpus)
        raw_scores = bm25.get_scores(query_tokens)
        ranked = sorted(
            (
                (max(0.0, float(score)), chunk)
                for chunk, score in zip(allowed_chunks, raw_scores, strict=True)
            ),
            key=lambda item: (-item[0], item[1].chunk_id),
        )
        return [
            BM25SearchHit(chunk=chunk, score=score)
            for score, chunk in ranked[:limit]
            if score > 0.0
        ]


__all__ = ["BM25Index", "tokenize_chinese"]
