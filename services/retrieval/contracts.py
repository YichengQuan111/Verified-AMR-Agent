"""P0-07 文档、分块、检索、引用与索引报告的严格数据契约。

这些模型是 Loader、Chunker、Qdrant、BM25、评测器和后续工具注册表之间的
公共边界。所有字段都拒绝额外输入，避免 payload 或评测 JSON 静默漂移。
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent.context import ContextEvidence, EvidenceSourceType
from agent.tools import UserRole


class RetrievalContract(BaseModel):
    """检索层契约共同基类。"""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        validate_default=True,
    )


class KnowledgeDocument(RetrievalContract):
    """通过 YAML Front Matter 校验后的冻结 Markdown 文档。"""

    doc_id: str = Field(min_length=1, max_length=128)
    filename: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=256)
    version: str = Field(min_length=1, max_length=64)
    role_scope: list[UserRole] = Field(min_length=1)
    source: str = Field(min_length=1, max_length=256)
    status: Literal["frozen"] = "frozen"
    checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    frozen_at: date | None = None
    text: str = Field(min_length=1)
    raw_content: bytes = Field(min_length=1)

    @model_validator(mode="after")
    def validate_roles(self) -> "KnowledgeDocument":
        """ACL 列表不能重复，否则过滤与审计会出现歧义。"""

        if len(self.role_scope) != len(set(self.role_scope)):
            raise ValueError("role_scope 不能包含重复角色")
        return self


class LoadedCorpus(RetrievalContract):
    """一次目录加载的有效文档和明确跳过项。"""

    documents: list[KnowledgeDocument]
    skipped_files: list[str]


class KnowledgeChunk(RetrievalContract):
    """可同时进入向量库和 BM25 的最小可引用知识单元。"""

    chunk_id: str = Field(min_length=1, max_length=256)
    doc_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=256)
    section: str = Field(min_length=1, max_length=256)
    version: str = Field(min_length=1, max_length=64)
    role_scope: list[UserRole] = Field(min_length=1)
    source: str = Field(min_length=1, max_length=256)
    checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    text: str = Field(min_length=1)
    section_ordinal: int = Field(ge=0)
    part_ordinal: int = Field(ge=0)
    frozen_at: date | None = None

    @model_validator(mode="after")
    def validate_roles(self) -> "KnowledgeChunk":
        """chunk ACL 必须保持集合语义，禁止重复角色。"""

        if len(self.role_scope) != len(set(self.role_scope)):
            raise ValueError("role_scope 不能包含重复角色")
        return self


class VectorSearchHit(RetrievalContract):
    """Qdrant 已在服务端应用 ACL 后返回的一个候选。"""

    chunk: KnowledgeChunk
    score: float


class BM25SearchHit(RetrievalContract):
    """对 ACL 预过滤语料建立 BM25 后返回的一个候选。"""

    chunk: KnowledgeChunk
    score: float = Field(ge=0.0)


class RetrievalResult(RetrievalContract):
    """带完整来源、正文及两路分数的可追溯检索结果。"""

    chunk_id: str
    doc_id: str
    title: str
    section: str
    version: str
    role_scope: list[UserRole]
    source: str
    checksum: str
    text: str
    frozen_at: date | None = None
    citation: str = Field(min_length=1)
    hybrid_score: float = Field(ge=0.0, le=1.0)
    # 未进入某一路候选时原始分数记 0，而不是省略字段；下游引用始终能看到
    # hybrid/vector/BM25 三个数值，归一化字段再说明融合使用的实际贡献。
    vector_score: float = 0.0
    bm25_score: float = Field(default=0.0, ge=0.0)
    normalized_vector_score: float = Field(ge=0.0, le=1.0)
    normalized_bm25_score: float = Field(ge=0.0, le=1.0)

    def to_context_evidence(
        self,
        *,
        collected_at: datetime | None = None,
    ) -> ContextEvidence:
        """转换为 P0-05 允许进入 Prompt 的 RAG 证据信封。

        ``source_id`` 使用 chunk_id 而不是 doc_id，避免同一文档多个 section 在
        ``NodeContext`` 中被误判为重复来源；文档身份仍完整保存在 citation/content。
        """

        collected = collected_at or datetime.now(timezone.utc)
        observed = (
            datetime.combine(self.frozen_at, time.min, tzinfo=timezone.utc)
            if self.frozen_at is not None
            else collected
        )
        return ContextEvidence(
            source_type=EvidenceSourceType.RAG,
            source_id=self.chunk_id,
            source_version=self.version,
            observed_at=observed,
            collected_at=collected,
            citation=self.citation,
            content={
                "doc_id": self.doc_id,
                "title": self.title,
                "section": self.section,
                "text": self.text,
                "hybrid_score": self.hybrid_score,
                "vector_score": self.vector_score,
                "bm25_score": self.bm25_score,
            },
        )


class RetrievalStatus(str, Enum):
    """检索链路唯一允许的证据结论。"""

    ANSWERABLE = "answerable"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class RetrievalResponse(RetrievalContract):
    """阈值判定后的安全响应；证据不足时不向下游暴露弱候选正文。"""

    query: str = Field(min_length=1)
    role_scope: UserRole
    status: RetrievalStatus
    reason: str = Field(min_length=1)
    top_k: int = Field(ge=1)
    minimum_hybrid_score: float = Field(ge=0.0, le=1.0)
    minimum_vector_score: float = Field(ge=-1.0, le=1.0)
    top_candidate_score: float | None = Field(default=None, ge=0.0, le=1.0)
    top_candidate_vector_score: float | None = Field(default=None, ge=-1.0, le=1.0)
    results: list[RetrievalResult]

    @model_validator(mode="after")
    def validate_evidence_boundary(self) -> "RetrievalResponse":
        """拒答时强制清空正文，防止调用方忽略 status 继续喂给模型。"""

        if self.status is RetrievalStatus.ANSWERABLE:
            if not self.results:
                raise ValueError("answerable 响应必须包含检索结果")
            if self.top_candidate_score is None:
                raise ValueError("answerable 响应必须记录 top_candidate_score")
            if self.top_candidate_vector_score is None:
                raise ValueError("answerable 响应必须记录 top_candidate_vector_score")
        elif self.results:
            raise ValueError("insufficient_evidence 响应不能包含弱证据正文")
        return self

    def to_context_evidence(
        self,
        *,
        collected_at: datetime | None = None,
    ) -> list[ContextEvidence]:
        """只把已经通过证据阈值的结果转换给 P0-05。"""

        if self.status is RetrievalStatus.INSUFFICIENT_EVIDENCE:
            return []
        timestamp = collected_at or datetime.now(timezone.utc)
        return [item.to_context_evidence(collected_at=timestamp) for item in self.results]


class KnowledgeIndexReport(RetrievalContract):
    """一次可重复索引的结构化审计结果。"""

    collection_name: str
    rebuilt: bool
    document_count: int = Field(ge=0)
    chunk_count: int = Field(ge=0)
    skipped_files: list[str]
    embedding_model_path: str
    embedding_dimension: int = Field(gt=0)
    document_checksums: dict[str, str]
    postgres_synchronised: bool
    indexed_at: datetime


__all__ = [
    "BM25SearchHit",
    "KnowledgeChunk",
    "KnowledgeDocument",
    "KnowledgeIndexReport",
    "LoadedCorpus",
    "RetrievalResponse",
    "RetrievalResult",
    "RetrievalStatus",
    "VectorSearchHit",
]
