"""冻结 Markdown 文档加载器与 YAML Front Matter 安全解析。"""

from __future__ import annotations

import re
from hashlib import sha256
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from services.retrieval.contracts import KnowledgeDocument, LoadedCorpus


_FRONT_MATTER_PATTERN = re.compile(
    r"\A\ufeff?---\s*\r?\n(?P<yaml>.*?)\r?\n---\s*(?:\r?\n|\Z)",
    re.DOTALL,
)
_REQUIRED_FRONT_MATTER = {
    "doc_id",
    "title",
    "version",
    "role_scope",
    "source",
    "status",
}


class DocumentLoadError(ValueError):
    """文档编码、Front Matter 或冻结契约不合法。"""


class MarkdownDocumentLoader:
    """读取 UTF-8 Markdown，并且只把 ``status=frozen`` 纳入语料。

    YAML 使用 ``safe_load``，因此 Front Matter 不能构造 Python 对象或执行代码。
    checksum 始终基于原始文件字节计算，换行或元数据变化都会触发重新索引。
    """

    def load_file(self, path: str | Path) -> KnowledgeDocument | None:
        """加载单个文件；非 frozen 文档明确返回 ``None``。"""

        source_path = Path(path)
        if not source_path.is_file():
            raise DocumentLoadError(f"Markdown 文档不存在: {source_path}")
        return self.load_bytes(source_path.read_bytes(), filename=source_path.name)

    def load_bytes(
        self,
        content: bytes,
        *,
        filename: str,
        expected_checksum: str | None = None,
    ) -> KnowledgeDocument | None:
        """解析数据库或文件系统提供的原始字节，并可校验外部 checksum。"""

        checksum = sha256(content).hexdigest()
        if expected_checksum is not None and checksum != expected_checksum:
            raise DocumentLoadError(
                f"文档 checksum 与持久化元数据不一致: {filename}"
            )
        try:
            decoded = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DocumentLoadError(f"Markdown 必须使用 UTF-8: {filename}") from exc

        match = _FRONT_MATTER_PATTERN.match(decoded)
        if match is None:
            raise DocumentLoadError(f"缺少合法 YAML Front Matter: {filename}")
        try:
            metadata = yaml.safe_load(match.group("yaml"))
        except yaml.YAMLError as exc:
            raise DocumentLoadError(f"YAML Front Matter 解析失败: {filename}") from exc
        if not isinstance(metadata, dict):
            raise DocumentLoadError(f"YAML Front Matter 必须是对象: {filename}")

        missing = sorted(_REQUIRED_FRONT_MATTER - set(metadata))
        if missing:
            raise DocumentLoadError(
                f"Front Matter 缺少字段 {', '.join(missing)}: {filename}"
            )
        if metadata.get("status") != "frozen":
            # 非冻结文档不是错误，但必须在加载报告中显式列为跳过项。
            return None

        body = decoded[match.end() :].strip()
        if not body:
            raise DocumentLoadError(f"冻结文档正文不能为空: {filename}")
        payload: dict[str, Any] = {
            "doc_id": metadata["doc_id"],
            "filename": Path(filename).name,
            "title": metadata["title"],
            "version": metadata["version"],
            "role_scope": metadata["role_scope"],
            "source": metadata["source"],
            "status": metadata["status"],
            "checksum": checksum,
            "frozen_at": metadata.get("frozen_at"),
            "text": body,
            "raw_content": content,
        }
        try:
            return KnowledgeDocument.model_validate(payload)
        except ValidationError as exc:
            raise DocumentLoadError(f"冻结文档元数据不合法: {filename}: {exc}") from exc

    def load_directory(self, root: str | Path) -> LoadedCorpus:
        """按文件名稳定排序加载目录，并拒绝重复 ``doc_id``。"""

        directory = Path(root)
        if not directory.is_dir():
            raise DocumentLoadError(f"知识库目录不存在: {directory}")
        documents: list[KnowledgeDocument] = []
        skipped: list[str] = []
        seen_doc_ids: set[str] = set()
        for path in sorted(directory.glob("*.md"), key=lambda item: item.name):
            document = self.load_file(path)
            if document is None:
                skipped.append(path.name)
                continue
            if document.doc_id in seen_doc_ids:
                raise DocumentLoadError(f"知识库包含重复 doc_id: {document.doc_id}")
            seen_doc_ids.add(document.doc_id)
            documents.append(document)
        if not documents:
            raise DocumentLoadError(f"知识库没有 status=frozen 的 Markdown: {directory}")
        return LoadedCorpus(documents=documents, skipped_files=skipped)


__all__ = ["DocumentLoadError", "MarkdownDocumentLoader"]
