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
