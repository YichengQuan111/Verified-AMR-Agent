"""Qdrant collection 生命周期、幂等写入与服务端 ACL 向量检索。"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import NAMESPACE_URL, uuid5

import numpy as np
from qdrant_client import QdrantClient
from qdrant_client.http import models

from agent.tools import UserRole
from services.retrieval.contracts import KnowledgeChunk, VectorSearchHit


class QdrantVectorStore:
    """只管理一个 dense cosine collection，不在检索后补做 ACL 隐藏。"""

    def __init__(
        self,
        *,
        url: str,
        collection_name: str = "amr_warehouse_knowledge",
        client: QdrantClient | None = None,
    ) -> None:
        if not collection_name:
            raise ValueError("collection_name 不能为空")
        self.collection_name = collection_name
        self.client = client or QdrantClient(url=url, timeout=30)

    def initialise_collection(self, dimension: int, *, rebuild: bool) -> None:
        """创建或验证 collection；rebuild 只删除精确命名的目标 collection。"""

        if dimension <= 0:
            raise ValueError("dimension 必须大于 0")
        exists = self.client.collection_exists(self.collection_name)
        if rebuild and exists:
            # 目标名称来自类型化配置，不使用通配符，也不触碰其他 collection。
            self.client.delete_collection(self.collection_name)
            exists = False
        if not exists:
            self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config=models.VectorParams(
                    size=dimension,
                    distance=models.Distance.COSINE,
                ),
            )
            # role_scope 必须有 keyword index，保证 ACL 在 Qdrant query_filter 内执行。
            for field_name in ("role_scope", "doc_id", "version"):
                self.client.create_payload_index(
                    collection_name=self.collection_name,
                    field_name=field_name,
                    field_schema=models.PayloadSchemaType.KEYWORD,
                    wait=True,
                )
            return
        self._validate_existing_collection(dimension)

    def _validate_existing_collection(self, dimension: int) -> None:
        """拒绝把新模型向量写进维度/距离不兼容的旧 collection。"""

        info = self.client.get_collection(self.collection_name)
        vectors = info.config.params.vectors
        if isinstance(vectors, dict):
            raise ValueError("P0-07 collection 不支持命名向量配置")
        if vectors.size != dimension:
            raise ValueError(
                f"Qdrant dimension 不匹配: {vectors.size} != {dimension}; "
                "请使用 rebuild"
            )
        if vectors.distance != models.Distance.COSINE:
            raise ValueError("P0-07 collection 必须使用 cosine distance")

    def index_chunks(
        self,
        chunks: Sequence[KnowledgeChunk],
        vectors: np.ndarray,
        *,
        rebuild: bool,
        batch_size: int = 64,
    ) -> None:
        """写入完整 payload；非 rebuild 时先替换本批文档的全部旧 chunks。"""

        if not chunks:
            raise ValueError("不能索引空 chunk 集合")
        matrix = np.asarray(vectors, dtype=np.float32)
        if matrix.ndim != 2 or matrix.shape[0] != len(chunks):
            raise ValueError("chunk 数量与向量矩阵行数不一致")
        if batch_size < 1:
            raise ValueError("batch_size 必须大于 0")
        self.initialise_collection(matrix.shape[1], rebuild=rebuild)

        if not rebuild:
            document_ids = sorted({chunk.doc_id for chunk in chunks})
            selector = models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="doc_id",
                            match=models.MatchAny(any=document_ids),
                        )
                    ]
                )
            )
            # 删除的是这些 doc_id 的旧点，防止 section 缩短后残留陈旧 chunk。
            self.client.delete(
                collection_name=self.collection_name,
                points_selector=selector,
                wait=True,
            )

        points: list[models.PointStruct] = []
        for chunk, vector in zip(chunks, matrix, strict=True):
            point_id = str(
                uuid5(NAMESPACE_URL, f"{self.collection_name}/{chunk.chunk_id}")
            )
            points.append(
                models.PointStruct(
                    id=point_id,
                    vector=vector.tolist(),
                    payload=chunk.model_dump(mode="json"),
                )
            )
        for start in range(0, len(points), batch_size):
            self.client.upsert(
                collection_name=self.collection_name,
                points=points[start : start + batch_size],
                wait=True,
            )

    def search(
        self,
        query_vector: np.ndarray,
        *,
        role_scope: UserRole,
        limit: int,
        document_ids: Sequence[str] | None = None,
    ) -> list[VectorSearchHit]:
        """在 Qdrant 内用 payload filter 先执行 ACL，再计算返回 Top-N。"""

        if limit < 1:
            raise ValueError("limit 必须大于 0")
        vector = np.asarray(query_vector, dtype=np.float32)
        if vector.ndim != 1:
            raise ValueError("query_vector 必须是一维向量")
        conditions: list[models.Condition] = [
            models.FieldCondition(
                key="role_scope",
                match=models.MatchValue(value=role_scope.value),
            )
        ]
        if document_ids is not None:
            allowed_ids = sorted(set(document_ids))
            if not allowed_ids:
                return []
            conditions.append(
                models.FieldCondition(
                    key="doc_id",
                    match=models.MatchAny(any=allowed_ids),
                )
            )
        response = self.client.query_points(
            collection_name=self.collection_name,
            query=vector,
            query_filter=models.Filter(must=conditions),
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        hits: list[VectorSearchHit] = []
        for point in response.points:
            if point.payload is None:
                raise ValueError(f"Qdrant point 缺少 payload: {point.id}")
            chunk = KnowledgeChunk.model_validate(point.payload)
            # 这是对服务端过滤结果的防御性断言，不是检索后的 ACL 实现。
            if role_scope not in chunk.role_scope:
                raise RuntimeError(f"Qdrant ACL filter 返回越权 chunk: {chunk.chunk_id}")
            hits.append(VectorSearchHit(chunk=chunk, score=float(point.score)))
        return hits

    def count(self) -> int:
        """返回 collection 当前点数，供幂等索引测试和运维检查。"""

        result = self.client.count(
            collection_name=self.collection_name,
            exact=True,
        )
        return int(result.count)


__all__ = ["QdrantVectorStore"]
