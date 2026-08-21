"""冻结知识库索引编排，以及检索运行时的统一构造入口。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from services.application import DocumentMetadataInput, DocumentService
from services.config.settings import RetrievalSettings
from services.retrieval.bm25 import BM25Index
from services.retrieval.chunking import MarkdownChunker
from services.retrieval.contracts import (
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeIndexReport,
    LoadedCorpus,
)
from services.retrieval.embedding import Embedder
from services.retrieval.hybrid import HybridRetriever
from services.retrieval.loader import MarkdownDocumentLoader
from services.retrieval.vector_store import QdrantVectorStore


class WarehouseKnowledgeIndexer:
    """串联 Loader → PostgreSQL → Chunker → Embedder → Qdrant。

    PostgreSQL 同步完成但 Qdrant 写入失败时，不会写 ``indexed_at``。下一次运行可
    使用相同 doc_id/checksum 安全重试；Qdrant point id 也由 chunk_id 确定性派生。
    """

    def __init__(
        self,
        settings: RetrievalSettings,
        *,
        document_service: DocumentService | None = None,
        loader: MarkdownDocumentLoader | None = None,
        chunker: MarkdownChunker | None = None,
        embedder: Embedder | None = None,
        vector_store: QdrantVectorStore | None = None,
    ) -> None:
        self.settings = settings
        self.document_service = document_service
        self.loader = loader or MarkdownDocumentLoader()
        self.chunker = chunker or MarkdownChunker(
            max_chars=settings.chunk_max_chars
        )
        self.embedder = embedder or Embedder(
            settings.embedding_model_path,
            device=settings.embedding_device,
            batch_size=settings.embedding_batch_size,
        )
        self.vector_store = vector_store or QdrantVectorStore(
            url=settings.qdrant_url,
            api_key=(
                settings.qdrant_api_key.get_secret_value()
                if settings.qdrant_api_key is not None
                else None
            ),
            collection_name=settings.collection_name,
        )

    def index_directory(
        self,
        knowledge_root: str | Path,
        *,
        rebuild: bool = True,
    ) -> KnowledgeIndexReport:
        """索引一个目录内全部 frozen Markdown，并返回可机器审计报告。"""

        corpus = self.loader.load_directory(knowledge_root)
        documents = corpus.documents
        if self.document_service is not None:
            documents = self._synchronise_and_reload(documents)
        chunks = self.chunker.chunk_documents(documents)
        vectors = self.embedder.embed_documents([chunk.text for chunk in chunks])
        self.vector_store.index_chunks(chunks, vectors, rebuild=rebuild)

        indexed_at = datetime.now(timezone.utc)
        if self.document_service is not None:
            self.document_service.mark_documents_indexed(
                [document.doc_id for document in documents],
                indexed_at=indexed_at,
            )
        return KnowledgeIndexReport(
            collection_name=self.settings.collection_name,
            rebuilt=rebuild,
            document_count=len(documents),
            chunk_count=len(chunks),
            skipped_files=corpus.skipped_files,
            embedding_model_path=str(self.embedder.model_path),
            embedding_dimension=self.embedder.dimension,
            document_checksums={
                document.doc_id: document.checksum for document in documents
            },
            postgres_synchronised=self.document_service is not None,
            indexed_at=indexed_at,
        )

    def _synchronise_and_reload(
        self,
        documents: list[KnowledgeDocument],
    ) -> list[KnowledgeDocument]:
        """通过 P0-06 Service 持久化并重新读取正文，避免旁路 documents 表。"""

        assert self.document_service is not None
        reloaded: list[KnowledgeDocument] = []
        for document in documents:
            self.document_service.upsert_frozen_knowledge_document(
                document.doc_id,
                DocumentMetadataInput(
                    filename=document.filename,
                    content_type="text/markdown",
                    version=document.version,
                    role_scope=document.role_scope,
                    source=document.source,
                    metadata={
                        "doc_id": document.doc_id,
                        "title": document.title,
                        "front_matter_status": document.status,
                        "frozen_at": (
                            document.frozen_at.isoformat()
                            if document.frozen_at is not None
                            else None
                        ),
                        "managed_by": "p007-warehouse-rag",
                    },
                ),
                document.raw_content,
            )
            stored = self.document_service.get_document(document.doc_id)
            parsed = self.loader.load_bytes(
                stored.content,
                filename=stored.metadata.filename,
                expected_checksum=stored.metadata.checksum,
            )
            if parsed is None:
                raise RuntimeError(
                    f"持久化后文档不再是 frozen: {document.doc_id}"
                )
            if (
                parsed.version != stored.metadata.version
                or parsed.role_scope != stored.metadata.role_scope
                or parsed.source != stored.metadata.source
            ):
                raise RuntimeError(
                    f"documents 关系化元数据与 Front Matter 不一致: {document.doc_id}"
                )
            reloaded.append(parsed)
        return reloaded


def load_knowledge_chunks(
    knowledge_root: str | Path,
    settings: RetrievalSettings,
    *,
    loader: MarkdownDocumentLoader | None = None,
    chunker: MarkdownChunker | None = None,
) -> tuple[LoadedCorpus, list[KnowledgeChunk]]:
    """加载同一份冻结语料供进程内 BM25 使用。"""

    active_loader = loader or MarkdownDocumentLoader()
    corpus = active_loader.load_directory(knowledge_root)
    active_chunker = chunker or MarkdownChunker(max_chars=settings.chunk_max_chars)
    return corpus, active_chunker.chunk_documents(corpus.documents)


def build_hybrid_retriever(
    settings: RetrievalSettings,
    knowledge_root: str | Path,
    *,
    embedder: Embedder | None = None,
    vector_store: QdrantVectorStore | None = None,
) -> HybridRetriever:
    """构造查询运行时；Qdrant dense 索引需先由索引器创建。"""

    _, chunks = load_knowledge_chunks(knowledge_root, settings)
    active_embedder = embedder or Embedder(
        settings.embedding_model_path,
        device=settings.embedding_device,
        batch_size=settings.embedding_batch_size,
    )
    active_store = vector_store or QdrantVectorStore(
        url=settings.qdrant_url,
        api_key=(
            settings.qdrant_api_key.get_secret_value()
            if settings.qdrant_api_key is not None
            else None
        ),
        collection_name=settings.collection_name,
    )
    return HybridRetriever(
        embedder=active_embedder,
        vector_store=active_store,
        bm25_index=BM25Index(chunks),
        vector_weight=settings.vector_weight,
        bm25_weight=settings.bm25_weight,
        candidate_multiplier=settings.candidate_multiplier,
        bm25_saturation=settings.bm25_saturation,
        minimum_hybrid_score=settings.minimum_hybrid_score,
        minimum_vector_score=settings.minimum_vector_score,
    )


__all__ = [
    "WarehouseKnowledgeIndexer",
    "build_hybrid_retriever",
    "load_knowledge_chunks",
]
