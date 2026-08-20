"""文档上传与元数据查询 HTTP 路由。"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import ValidationError

from agent.tools import UserRole
from apps.api.dependencies import get_document_service
from services.application import (
    DocumentMetadataInput,
    DocumentService,
    DocumentView,
    MAX_DOCUMENT_BYTES,
)


router = APIRouter(prefix="/documents", tags=["documents"])


def _parse_document_metadata(
    *,
    filename: str,
    content_type: str,
    version: str,
    role_scope: str,
    source: str,
    metadata_json: str,
) -> DocumentMetadataInput:
    """把 multipart 文本字段转换为严格 Pydantic 元数据。"""

    try:
        parsed_metadata = json.loads(metadata_json)
        if not isinstance(parsed_metadata, dict):
            raise ValueError("metadata_json 必须是 JSON 对象")
        roles = [UserRole(item.strip()) for item in role_scope.split(",") if item.strip()]
        return DocumentMetadataInput(
            filename=filename,
            content_type=content_type or "application/octet-stream",
            version=version,
            role_scope=roles,
            source=source,
            metadata=parsed_metadata,
        )
    except (json.JSONDecodeError, ValueError, ValidationError) as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_DOCUMENT_METADATA", "message": str(exc)},
        ) from exc


@router.post("", response_model=DocumentView, status_code=status.HTTP_201_CREATED)
async def upload_document(
    file: UploadFile = File(...),
    version: str = Form(...),
    role_scope: str = Form(default="viewer"),
    source: str = Form(...),
    metadata_json: str = Form(default="{}"),
    service: DocumentService = Depends(get_document_service),
) -> DocumentView:
    """上传不超过 10 MiB 的文档，并返回不含正文的元数据。"""

    metadata = _parse_document_metadata(
        filename=file.filename or "",
        content_type=file.content_type or "application/octet-stream",
        version=version,
        role_scope=role_scope,
        source=source,
        metadata_json=metadata_json,
    )
    try:
        # 多读 1 字节即可识别超限，不会把任意大请求完整载入内存。
        content = await file.read(MAX_DOCUMENT_BYTES + 1)
    finally:
        await file.close()
    return await asyncio.to_thread(service.create_document, metadata, content)


@router.get("/{document_id}", response_model=DocumentView)
async def get_document(
    document_id: str,
    service: DocumentService = Depends(get_document_service),
) -> DocumentView:
    """查询文档元数据；原始正文只提供给后续受控索引 Service。"""

    stored = await asyncio.to_thread(service.get_document, document_id)
    return stored.metadata


__all__ = ["router"]
