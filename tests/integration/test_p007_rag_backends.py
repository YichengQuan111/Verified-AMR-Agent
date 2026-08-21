"""P0-07 真实 PostgreSQL 文档状态与 Qdrant payload ACL 集成测试。"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import numpy as np
import pytest
from qdrant_client import QdrantClient
from sqlalchemy import delete

from agent.tools import UserRole
from services.application import DocumentMetadataInput, DocumentService
from services.config.settings import load_settings
from services.persistence import (
    DocumentRecord,
    create_database_runtime,
)
from services.retrieval import KnowledgeChunk, QdrantVectorStore


@pytest.fixture(scope="module")
def database_runtime() -> Iterator[object]:
    """复用已迁移 PostgreSQL；服务不可用时应真实失败。"""

    runtime = create_database_runtime(load_settings().database)
    yield runtime
    runtime.dispose()


def _knowledge_metadata(document_id: str) -> DocumentMetadataInput:
    """生成 P0-07 受管文档标记。"""

    return DocumentMetadataInput(
        filename=f"{document_id}.md",
        content_type="text/markdown",
        version="1.0-frozen",
        role_scope=[UserRole.VIEWER, UserRole.OPERATOR],
        source="p007-integration-test",
        metadata={
            "doc_id": document_id,
            "title": "P0-07 集成测试文档",
            "front_matter_status": "frozen",
            "frozen_at": "2026-08-20",
            "managed_by": "p007-warehouse-rag",
        },
    )


def test_postgres_frozen_document_upsert_and_index_marker_are_idempotent(
    database_runtime: object,
) -> None:
    """内容未变时保留 indexed_at，内容变化后必须重新等待索引。"""

    document_id = f"p007_test_{uuid4().hex}"
    service = DocumentService(database_runtime.session_factory)
    metadata = _knowledge_metadata(document_id)
    first_content = b"---\nstatus: frozen\n---\n\n# first"
    second_content = b"---\nstatus: frozen\n---\n\n# second"
    try:
        created = service.upsert_frozen_knowledge_document(
            document_id,
            metadata,
            first_content,
        )
        assert created.status == "frozen"
        assert created.indexed_at is None

        marked = service.mark_documents_indexed([document_id])[0]
        assert marked.status == "indexed"
        assert marked.indexed_at is not None

        unchanged = service.upsert_frozen_knowledge_document(
            document_id,
            metadata,
            first_content,
        )
        assert unchanged.status == "indexed"
        assert unchanged.indexed_at == marked.indexed_at

        changed = service.upsert_frozen_knowledge_document(
            document_id,
            metadata,
            second_content,
        )
        assert changed.status == "frozen"
        assert changed.indexed_at is None
        assert changed.checksum != created.checksum
    finally:
        with database_runtime.session_factory() as session:
            with session.begin():
                session.execute(
                    delete(DocumentRecord).where(
                        DocumentRecord.document_id == document_id
                    )
                )


def _chunk(
    chunk_id: str,
    doc_id: str,
    roles: list[UserRole],
    text: str,
) -> KnowledgeChunk:
    """生成真实 Qdrant payload。"""

    return KnowledgeChunk(
        chunk_id=chunk_id,
        doc_id=doc_id,
        title=doc_id,
        section="1. 测试",
        version="1.0-frozen",
        role_scope=roles,
        source="p007-integration-test",
        checksum="b" * 64,
        text=text,
        section_ordinal=1,
        part_ordinal=0,
        frozen_at="2026-08-20",
    )


def test_qdrant_rebuild_idempotency_and_payload_acl() -> None:
    """viewer 查询的 Qdrant response 中不能出现 operator-only points。"""

    collection = f"p007_test_{uuid4().hex}"
    retrieval = load_settings().retrieval
    # 发布 Compose 给 Qdrant 配了 API key 后，匿名客户端会被拒绝；测试必须
    # 使用与 check_qdrant.py 相同的凭据来源，不能再直连默认无鉴权实例。
    qdrant_key = (
        retrieval.qdrant_api_key.get_secret_value()
        if retrieval.qdrant_api_key is not None
        else None
    )
    client = QdrantClient(url=retrieval.qdrant_url, api_key=qdrant_key, timeout=10)
    store = QdrantVectorStore(
        url=retrieval.qdrant_url,
        api_key=qdrant_key,
        collection_name=collection,
        client=client,
    )
    public = _chunk(
        "public-chunk",
        "public-doc",
        [UserRole.VIEWER, UserRole.OPERATOR],
        "公开仓储规则",
    )
    restricted_a = _chunk(
        "restricted-a",
        "restricted-doc",
        [UserRole.OPERATOR],
        "operator 审批秘密 A",
    )
    restricted_b = _chunk(
        "restricted-b",
        "restricted-doc",
        [UserRole.OPERATOR],
        "operator 审批秘密 B",
    )
    chunks = [public, restricted_a, restricted_b]
    vectors = np.asarray(
        [[1.0, 0.0, 0.0], [0.99, 0.01, 0.0], [0.98, 0.02, 0.0]],
        dtype=np.float32,
    )
    try:
        store.index_chunks(chunks, vectors, rebuild=True)
        assert store.count() == 3

        viewer_hits = store.search(
            np.asarray([1.0, 0.0, 0.0], dtype=np.float32),
            role_scope=UserRole.VIEWER,
            limit=10,
        )
        assert [hit.chunk.doc_id for hit in viewer_hits] == ["public-doc"]

        operator_hits = store.search(
            np.asarray([1.0, 0.0, 0.0], dtype=np.float32),
            role_scope=UserRole.OPERATOR,
            limit=10,
            document_ids=["restricted-doc"],
        )
        assert {hit.chunk.chunk_id for hit in operator_hits} == {
            "restricted-a",
            "restricted-b",
        }

        # 同一 doc_id 批次非 rebuild 写入会先删旧点再 upsert，数量不会增长。
        store.index_chunks(chunks, vectors, rebuild=False)
        assert store.count() == 3
    finally:
        if client.collection_exists(collection):
            # 只删除本测试 UUID 命名的 collection，不影响正式知识库。
            client.delete_collection(collection)
