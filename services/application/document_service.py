"""P0-06 文档上传与查询应用服务。"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from sqlalchemy.exc import IntegrityError

from services.application.contracts import (
    DocumentMetadataInput,
    DocumentView,
    StoredDocument,
)
from services.application.exceptions import (
    DocumentTooLargeError,
    InvalidDocumentError,
    PersistenceConflictError,
    ResourceNotFoundError,
)
from services.application.run_service import IdentifierFactory, new_identifier
from services.persistence import DocumentRecord, DocumentRepository, SessionFactory


MAX_DOCUMENT_BYTES = 10 * 1024 * 1024


class DocumentService:
    """保存文档关系化索引、完整元数据快照与原始内容。"""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        identifier_factory: IdentifierFactory = new_identifier,
    ) -> None:
        self._session_factory = session_factory
        self._new_id = identifier_factory

    def create_document(
        self,
        metadata: DocumentMetadataInput,
        content: bytes,
    ) -> DocumentView:
        """校验大小并在单事务中写入 documents。"""

        if not content:
            raise InvalidDocumentError("文档内容不能为空")
        if len(content) > MAX_DOCUMENT_BYTES:
            raise DocumentTooLargeError(
                f"文档超过 {MAX_DOCUMENT_BYTES} 字节上限"
            )

        # 只保留文件名，浏览器提供的 C:\\fakepath 或恶意相对路径不能进入存储键。
        safe_filename = Path(metadata.filename).name
        if safe_filename in {"", ".", ".."}:
            raise InvalidDocumentError("文档文件名无效")

        now = datetime.now(timezone.utc)
        document_id = self._new_id("document")
        checksum = sha256(content).hexdigest()
        metadata_snapshot = metadata.model_copy(
            update={"filename": safe_filename}
        ).model_dump(mode="json")
        record = DocumentRecord(
            document_id=document_id,
            filename=safe_filename,
            content_type=metadata.content_type,
            status="stored",
            version=metadata.version,
            role_scope=[role.value for role in metadata.role_scope],
            source=metadata.source,
            checksum=checksum,
            size_bytes=len(content),
            storage_uri=f"postgresql://documents/{document_id}/content",
            content=content,
            metadata_snapshot=metadata_snapshot,
            created_at=now,
            updated_at=now,
            indexed_at=None,
        )
        try:
            with self._session_factory() as session:
                with session.begin():
                    DocumentRepository(session).add(record)
                    session.flush()
                    result = self._to_document_view(record)
        except IntegrityError as exc:
            raise PersistenceConflictError("文档写入冲突，事务已回滚") from exc
        return result

    def get_document(self, document_id: str) -> StoredDocument:
        """返回元数据和正文；Router 默认只暴露元数据。"""

        with self._session_factory() as session:
            record = DocumentRepository(session).get(document_id)
            if record is None:
                raise ResourceNotFoundError(f"文档不存在: {document_id}")
            return StoredDocument(
                metadata=self._to_document_view(record),
                content=record.content,
            )

    def upsert_frozen_knowledge_document(
        self,
        document_id: str,
        metadata: DocumentMetadataInput,
        content: bytes,
    ) -> DocumentView:
        """把受版本控制的 frozen Markdown 幂等同步到既有 documents 表。

        P0-07 使用 Front Matter 的 ``doc_id`` 作为稳定主键。只有带匹配 doc_id 和
        ``front_matter_status=frozen`` 标记的受管文档能调用此入口，避免索引器
        覆盖普通上传文档。内容或 ACL 变化会清空 indexed_at，等待 Qdrant 成功后
        由 ``mark_documents_indexed`` 再原子更新状态。
        """

        if not document_id or len(document_id) > 64:
            raise InvalidDocumentError("知识文档 doc_id 长度必须为 1..64")
        if not content:
            raise InvalidDocumentError("文档内容不能为空")
        if len(content) > MAX_DOCUMENT_BYTES:
            raise DocumentTooLargeError(
                f"文档超过 {MAX_DOCUMENT_BYTES} 字节上限"
            )
        marker = metadata.metadata
        if marker.get("doc_id") != document_id:
            raise InvalidDocumentError("metadata.doc_id 必须与 document_id 一致")
        if marker.get("front_matter_status") != "frozen":
            raise InvalidDocumentError("只允许同步 status=frozen 的知识文档")

        safe_filename = Path(metadata.filename).name
        if safe_filename in {"", ".", ".."}:
            raise InvalidDocumentError("文档文件名无效")
        checksum = sha256(content).hexdigest()
        now = datetime.now(timezone.utc)
        snapshot = metadata.model_copy(
            update={"filename": safe_filename}
        ).model_dump(mode="json")
        try:
            with self._session_factory() as session:
                with session.begin():
                    repository = DocumentRepository(session)
                    record = repository.get(document_id, for_update=True)
                    if record is None:
                        record = DocumentRecord(
                            document_id=document_id,
                            filename=safe_filename,
                            content_type=metadata.content_type,
                            status="frozen",
                            version=metadata.version,
                            role_scope=[role.value for role in metadata.role_scope],
                            source=metadata.source,
                            checksum=checksum,
                            size_bytes=len(content),
                            storage_uri=(
                                f"postgresql://documents/{document_id}/content"
                            ),
                            content=content,
                            metadata_snapshot=snapshot,
                            created_at=now,
                            updated_at=now,
                            indexed_at=None,
                        )
                        repository.add(record)
                    else:
                        previous_marker = record.metadata_snapshot.get("metadata", {})
                        if previous_marker.get("doc_id") != document_id:
                            raise PersistenceConflictError(
                                f"document_id 已被非 P0-07 文档占用: {document_id}"
                            )
                        changed = any(
                            (
                                record.filename != safe_filename,
                                record.content_type != metadata.content_type,
                                record.version != metadata.version,
                                record.role_scope
                                != [role.value for role in metadata.role_scope],
                                record.source != metadata.source,
                                record.checksum != checksum,
                                record.metadata_snapshot != snapshot,
                            )
                        )
                        record.filename = safe_filename
                        record.content_type = metadata.content_type
                        record.version = metadata.version
                        record.role_scope = [
                            role.value for role in metadata.role_scope
                        ]
                        record.source = metadata.source
                        record.checksum = checksum
                        record.size_bytes = len(content)
                        record.content = content
                        record.metadata_snapshot = snapshot
                        if changed:
                            record.status = "frozen"
                            record.indexed_at = None
                            record.updated_at = now
                    session.flush()
                    result = self._to_document_view(record)
        except IntegrityError as exc:
            raise PersistenceConflictError("知识文档同步冲突，事务已回滚") from exc
        return result

    def mark_documents_indexed(
        self,
        document_ids: list[str],
        *,
        indexed_at: datetime | None = None,
    ) -> list[DocumentView]:
        """Qdrant 全部写入成功后，在一个事务中标记本批文档已索引。"""

        unique_ids = sorted(set(document_ids))
        if not unique_ids:
            raise InvalidDocumentError("待标记的 document_ids 不能为空")
        timestamp = indexed_at or datetime.now(timezone.utc)
        results: list[DocumentView] = []
        with self._session_factory() as session:
            with session.begin():
                repository = DocumentRepository(session)
                records: list[DocumentRecord] = []
                for document_id in unique_ids:
                    record = repository.get(document_id, for_update=True)
                    if record is None:
                        raise ResourceNotFoundError(f"文档不存在: {document_id}")
                    marker = record.metadata_snapshot.get("metadata", {})
                    if marker.get("front_matter_status") != "frozen":
                        raise InvalidDocumentError(
                            f"非 frozen 知识文档不能标记索引: {document_id}"
                        )
                    records.append(record)
                # 先确认整批都存在且合法，再修改任何一行，避免部分标记成功。
                for record in records:
                    record.status = "indexed"
                    record.indexed_at = timestamp
                    record.updated_at = timestamp
                    results.append(self._to_document_view(record))
                session.flush()
        return results

    @staticmethod
    def _to_document_view(record: DocumentRecord) -> DocumentView:
        """把文档行映射为不包含正文的公共元数据。"""

        return DocumentView(
            document_id=record.document_id,
            filename=record.filename,
            content_type=record.content_type,
            status=record.status,
            version=record.version,
            role_scope=record.role_scope,
            source=record.source,
            checksum=record.checksum,
            size_bytes=record.size_bytes,
            storage_uri=record.storage_uri,
            metadata=record.metadata_snapshot.get("metadata", {}),
            created_at=record.created_at,
            updated_at=record.updated_at,
            indexed_at=record.indexed_at,
        )


__all__ = ["DocumentService", "MAX_DOCUMENT_BYTES"]
