"""P0-07 Loader、Chunker、Embedder、BM25、融合、ACL 与拒答单元测试。"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from pydantic import ValidationError

from agent.tools import UserRole
from evals.rag.run_eval import _load_cases
from services.config.settings import RetrievalSettings
from services.retrieval import (
    BM25Index,
    DocumentLoadError,
    Embedder,
    HybridRetriever,
    KnowledgeChunk,
    MarkdownChunker,
    MarkdownDocumentLoader,
    RetrievalStatus,
    VectorSearchHit,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
KNOWLEDGE_ROOT = PROJECT_ROOT / "domains" / "amr_warehouse" / "knowledge"


def _markdown(*, status: str = "frozen", body: str | None = None) -> bytes:
    """生成包含完整 Front Matter 的最小 Markdown。"""

    text = f"""---
doc_id: test_document_v1
title: 测试文档
version: 1.0-frozen
role_scope:
  - viewer
  - operator
source: pytest
status: {status}
frozen_at: 2026-08-20
---

# 测试文档

{body or '## 1. 规则\n\n第一条完整规则。'}
"""
    return text.encode("utf-8")


def _chunk(
    chunk_id: str,
    *,
    doc_id: str,
    text: str,
    roles: list[UserRole],
    section: str = "1. 规则",
) -> KnowledgeChunk:
    """构造通过严格契约的检索测试 chunk。"""

    return KnowledgeChunk(
        chunk_id=chunk_id,
        doc_id=doc_id,
        title=f"{doc_id} 标题",
        section=section,
        version="1.0-frozen",
        role_scope=roles,
        source="pytest",
        checksum="a" * 64,
        text=text,
        section_ordinal=1,
        part_ordinal=0,
        frozen_at="2026-08-20",
    )


def test_loader_parses_front_matter_checksum_and_skips_non_frozen(tmp_path: Path) -> None:
    """checksum 基于原始字节，draft 文件只能显式跳过。"""

    frozen = _markdown()
    (tmp_path / "a_frozen.md").write_bytes(frozen)
    (tmp_path / "b_draft.md").write_bytes(_markdown(status="draft"))

    corpus = MarkdownDocumentLoader().load_directory(tmp_path)

    assert len(corpus.documents) == 1
    assert corpus.skipped_files == ["b_draft.md"]
    document = corpus.documents[0]
    assert document.doc_id == "test_document_v1"
    assert document.role_scope == [UserRole.VIEWER, UserRole.OPERATOR]
    assert document.checksum == sha256(frozen).hexdigest()
    assert document.raw_content == frozen


def test_loader_rejects_missing_or_unsafe_front_matter() -> None:
    """缺 Front Matter 和 Python 对象 YAML 都不能进入索引。"""

    loader = MarkdownDocumentLoader()
    with pytest.raises(DocumentLoadError, match="缺少合法"):
        loader.load_bytes(b"# no metadata", filename="bad.md")
    unsafe = b"---\ndoc_id: !!python/object/apply:os.system ['echo bad']\n---\nbody"
    with pytest.raises(DocumentLoadError, match="YAML Front Matter"):
        loader.load_bytes(unsafe, filename="unsafe.md")


def test_chunker_prefers_h2_and_excludes_question_only_sections() -> None:
    """每个 H2 独立引用，H3 留在父 section，示例问题不作为证据。"""

    document = MarkdownDocumentLoader().load_bytes(
        _markdown(
            body=(
                "文档摘要。\n\n"
                "## 1. 第一节\n\n第一条规则。\n\n### 1.1 子节\n\n子节规则。\n\n"
                "## 2. 第二节\n\n第二条规则。\n\n"
                "## 3. RAG 示例问题\n\n1. 第一条规则是什么？"
            )
        ),
        filename="test.md",
    )
    assert document is not None

    chunks = MarkdownChunker(max_chars=1800).chunk_document(document)

    assert [chunk.section for chunk in chunks] == ["文档概述", "1. 第一节", "2. 第二节"]
    first_section = next(chunk for chunk in chunks if chunk.section == "1. 第一节")
    assert "### 1.1 子节" in first_section.text
    assert first_section.doc_id == document.doc_id
    assert first_section.checksum == document.checksum


def test_chunker_only_secondary_splits_long_section_at_semantic_boundaries() -> None:
    """短 section 不拆；超长 section 的句子尽量保持完整且 ID 稳定。"""

    long_body = "## 1. 长规则\n\n" + "\n\n".join(
        f"规则 {index} 必须完整执行，不能绕过验证。" for index in range(30)
    )
    document = MarkdownDocumentLoader().load_bytes(
        _markdown(body=long_body),
        filename="long.md",
    )
    assert document is not None
    chunker = MarkdownChunker(max_chars=256)

    first = chunker.chunk_document(document)
    second = chunker.chunk_document(document)

    assert len(first) > 1
    assert [item.chunk_id for item in first] == [item.chunk_id for item in second]
    assert all(item.section == "1. 长规则" for item in first)
    assert all(len(item.text) <= 256 for item in first)
    assert all("不能绕过验证。" in item.text for item in first)


class _FakeSentenceModel:
    """记录 prompt_name，并返回动态三维向量。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def get_embedding_dimension(self) -> int:
        return 3

    def encode(self, texts: list[str], **kwargs: Any) -> np.ndarray:
        self.calls.append({"texts": texts, **kwargs})
        return np.asarray([[1.0, float(len(text)), 0.5] for text in texts])


def test_embedder_uses_distinct_prompts_and_dynamic_dimension() -> None:
    """维度来自模型，文档/查询不能误用同一 prompt。"""

    model = _FakeSentenceModel()
    embedder = Embedder("unused-in-test", model=model, batch_size=2)

    documents = embedder.embed_documents(["文档一", "文档二"])
    query = embedder.embed_query("问题")

    assert embedder.dimension == 3
    assert documents.shape == (2, 3)
    assert query.shape == (3,)
    assert [call["prompt_name"] for call in model.calls] == ["document", "query"]
    assert all(call["normalize_embeddings"] is True for call in model.calls)


def test_bm25_acl_filters_corpus_before_scoring() -> None:
    """viewer 的 BM25 语料中从一开始就没有两份 operator-only 文档。"""

    corpus = MarkdownDocumentLoader().load_directory(KNOWLEDGE_ROOT)
    chunks = MarkdownChunker(max_chars=1800).chunk_documents(corpus.documents)
    index = BM25Index(chunks)

    viewer_hits = index.search(
        "审批超时 默认批准 旧计划版本",
        role_scope=UserRole.VIEWER,
        limit=20,
    )
    operator_hits = index.search(
        "审批超时 默认批准 旧计划版本",
        role_scope=UserRole.OPERATOR,
        limit=20,
    )

    restricted = {"amr_fault_handling_v1_0", "dispatch_approval_policy_v1_0"}
    assert not ({hit.chunk.doc_id for hit in viewer_hits} & restricted)
    assert "dispatch_approval_policy_v1_0" in {
        hit.chunk.doc_id for hit in operator_hits
    }


class _FakeQueryEmbedder:
    def embed_query(self, query: str) -> np.ndarray:
        assert query
        return np.asarray([1.0, 0.0, 0.0], dtype=np.float32)


class _RecordingVectorStore:
    """模拟已经在服务端过滤后的 vector hits，并记录 ACL 参数。"""

    def __init__(self, chunks: list[KnowledgeChunk]) -> None:
        self.chunks = chunks
        self.calls: list[tuple[UserRole, tuple[str, ...] | None]] = []

    def search(
        self,
        query_vector: np.ndarray,
        *,
        role_scope: UserRole,
        limit: int,
        document_ids: list[str] | None = None,
    ) -> list[VectorSearchHit]:
        assert query_vector.shape == (3,)
        self.calls.append(
            (role_scope, None if document_ids is None else tuple(document_ids))
        )
        allowed = [
            chunk
            for chunk in self.chunks
            if role_scope in chunk.role_scope
            and (document_ids is None or chunk.doc_id in document_ids)
        ]
        return [
            VectorSearchHit(chunk=chunk, score=0.8 - index * 0.1)
            for index, chunk in enumerate(allowed[:limit])
        ]


def test_hybrid_scores_citations_refusal_and_context_evidence() -> None:
    """融合分数可解释；拒答清空正文；通过阈值后可转换 P0-05 证据。"""

    public = _chunk(
        "public-1",
        doc_id="public_doc",
        text="公开运输流程需要确定性验证。",
        roles=[UserRole.VIEWER, UserRole.OPERATOR],
    )
    restricted = _chunk(
        "operator-1",
        doc_id="operator_doc",
        text="operator 审批策略。",
        roles=[UserRole.OPERATOR],
    )
    store = _RecordingVectorStore([public, restricted])
    bm25 = BM25Index([public, restricted])

    strict = HybridRetriever(
        embedder=_FakeQueryEmbedder(),
        vector_store=store,
        bm25_index=bm25,
        minimum_hybrid_score=0.99,
        minimum_vector_score=0.99,
    )
    refused = strict.retrieve(
        "公开运输流程",
        role_scope=UserRole.VIEWER,
        top_k=2,
    )
    assert refused.status is RetrievalStatus.INSUFFICIENT_EVIDENCE
    assert refused.results == []
    assert refused.to_context_evidence() == []

    permissive = HybridRetriever(
        embedder=_FakeQueryEmbedder(),
        vector_store=store,
        bm25_index=bm25,
        minimum_hybrid_score=0.0,
        minimum_vector_score=0.0,
    )
    response = permissive.retrieve(
        "公开运输流程",
        role_scope=UserRole.VIEWER,
        top_k=2,
        document_ids=["public_doc"],
    )
    assert response.status is RetrievalStatus.ANSWERABLE
    assert [result.doc_id for result in response.results] == ["public_doc"]
    result = response.results[0]
    assert 0.0 <= result.hybrid_score <= 1.0
    assert result.vector_score == pytest.approx(0.8)
    assert result.bm25_score >= 0.0
    assert result.chunk_id in result.citation
    evidence = response.to_context_evidence()
    assert len(evidence) == 1
    assert evidence[0].source_id == "public-1"
    assert evidence[0].content["doc_id"] == "public_doc"
    assert store.calls[-1] == (UserRole.VIEWER, ("public_doc",))


def test_retrieval_settings_validate_weights_and_calibrated_threshold() -> None:
    """默认阈值来自评测，非法权重组合在启动前失败。"""

    assert RetrievalSettings().minimum_hybrid_score == pytest.approx(0.809)
    assert RetrievalSettings().minimum_vector_score == pytest.approx(0.499)
    with pytest.raises(ValidationError, match="必须等于 1.0"):
        RetrievalSettings(vector_weight=0.7, bm25_weight=0.4)


def test_versioned_eval_set_has_twenty_cases_and_required_categories() -> None:
    """评测集数量与事实/改写/数值/跨文档/ACL/拒答类别不得回退。"""

    cases = _load_cases(PROJECT_ROOT / "evals" / "rag" / "cases.json")
    assert len(cases) == 20
    categories = {case.category for case in cases}
    assert {
        "explicit_fact",
        "semantic_paraphrase",
        "exact_numeric",
        "exact_keyword",
        "cross_document",
        "acl_operator",
        "acl_viewer",
        "unanswerable",
    }.issubset(categories)
    assert any(not case.answerable for case in cases)
