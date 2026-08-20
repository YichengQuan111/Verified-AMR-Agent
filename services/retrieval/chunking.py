"""按 Markdown 二级标题优先切分仓储 SOP，并保留规则语义边界。"""

from __future__ import annotations

import re
from hashlib import sha256

from services.retrieval.contracts import KnowledgeChunk, KnowledgeDocument


_H2_PATTERN = re.compile(r"^##(?!#)\s+(?P<title>.+?)\s*#*\s*$")
_H1_PATTERN = re.compile(r"^#(?!#)\s+")
_LOWER_HEADING_PATTERN = re.compile(r"^#{3,6}\s+")
_NON_EVIDENCE_SECTION_PATTERN = re.compile(r"^\d+\.\s*RAG 示例问题$")
_SEMANTIC_PUNCTUATION = "。！？；.!?;，,"


class MarkdownChunker:
    """先按 ``##`` 切 section，仅对超长 section 做第二级语义拆分。"""

    def __init__(self, *, max_chars: int = 1800) -> None:
        if max_chars < 256:
            raise ValueError("max_chars 不能小于 256")
        self.max_chars = max_chars

    def chunk_documents(
        self,
        documents: list[KnowledgeDocument],
    ) -> list[KnowledgeChunk]:
        """按输入顺序生成稳定 chunk；重复 chunk_id 会立即失败。"""

        chunks: list[KnowledgeChunk] = []
        seen_ids: set[str] = set()
        for document in documents:
            for chunk in self.chunk_document(document):
                if chunk.chunk_id in seen_ids:
                    raise ValueError(f"生成了重复 chunk_id: {chunk.chunk_id}")
                seen_ids.add(chunk.chunk_id)
                chunks.append(chunk)
        return chunks

    def chunk_document(self, document: KnowledgeDocument) -> list[KnowledgeChunk]:
        """把单份文档转换为 section-aware chunks。"""

        chunks: list[KnowledgeChunk] = []
        for section_ordinal, (section, content) in enumerate(
            self._extract_sections(document)
        ):
            parts = self._split_section(document.title, section, content)
            for part_ordinal, text in enumerate(parts):
                digest = sha256(text.encode("utf-8")).hexdigest()[:12]
                chunk_id = (
                    f"{document.doc_id}::{section_ordinal:02d}::"
                    f"{part_ordinal:02d}::{digest}"
                )
                chunks.append(
                    KnowledgeChunk(
                        chunk_id=chunk_id,
                        doc_id=document.doc_id,
                        title=document.title,
                        section=section,
                        version=document.version,
                        role_scope=document.role_scope,
                        source=document.source,
                        checksum=document.checksum,
                        text=text,
                        section_ordinal=section_ordinal,
                        part_ordinal=part_ordinal,
                        frozen_at=document.frozen_at,
                    )
                )
        if not chunks:
            raise ValueError(f"文档未生成任何 chunk: {document.doc_id}")
        return chunks

    @staticmethod
    def _extract_sections(document: KnowledgeDocument) -> list[tuple[str, str]]:
        """识别精确 H2；H3/H4 留在所属 H2 中，不打断局部规则。"""

        sections: list[tuple[str, str]] = []
        current_name = "文档概述"
        current_lines: list[str] = []

        def flush() -> None:
            content = "\n".join(current_lines).strip()
            # “RAG 示例问题”只有问题清单而没有规则答案，召回它会把问题本身
            # 误当成证据并抬高无答案分数，因此保留在源文档但不生成证据 chunk。
            if content and not _NON_EVIDENCE_SECTION_PATTERN.match(current_name):
                sections.append((current_name, content))

        for line in document.text.splitlines():
            match = _H2_PATTERN.match(line)
            if match is not None:
                flush()
                current_name = match.group("title").strip()
                current_lines = []
                continue
            # Front Matter 的 title 已作为 metadata 保存，正文首个 H1 不重复索引。
            if not sections and not current_lines and _H1_PATTERN.match(line):
                continue
            current_lines.append(line)
        flush()
        return sections

    def _split_section(self, title: str, section: str, content: str) -> list[str]:
        """短 section 保持整体；长 section 优先按段落/表格/列表块打包。"""

        prefix = f"# {title}\n\n## {section}\n\n"
        rendered = prefix + content.strip()
        if len(rendered) <= self.max_chars:
            return [rendered]

        content_limit = max(128, self.max_chars - len(prefix))
        blocks = self._semantic_blocks(content)
        expanded: list[str] = []
        for block in blocks:
            if len(block) <= content_limit:
                expanded.append(block)
            else:
                expanded.extend(self._split_oversized_block(block, content_limit))

        parts: list[str] = []
        current: list[str] = []
        current_length = 0
        for block in expanded:
            separator_length = 2 if current else 0
            if current and current_length + separator_length + len(block) > content_limit:
                parts.append(prefix + "\n\n".join(current))
                current = []
                current_length = 0
            current.append(block)
            current_length += (2 if current_length else 0) + len(block)
        if current:
            parts.append(prefix + "\n\n".join(current))
        return parts

    @staticmethod
    def _semantic_blocks(content: str) -> list[str]:
        """按空行形成语义块，并让 H3/H4 与其后正文保持在同一块。"""

        raw_blocks = [
            block.strip()
            for block in re.split(r"\r?\n\s*\r?\n", content.strip())
            if block.strip()
        ]
        result: list[str] = []
        pending_heading: str | None = None
        for block in raw_blocks:
            if _LOWER_HEADING_PATTERN.match(block) and "\n" not in block:
                if pending_heading is not None:
                    result.append(pending_heading)
                pending_heading = block
                continue
            if pending_heading is not None:
                block = f"{pending_heading}\n\n{block}"
                pending_heading = None
            result.append(block)
        if pending_heading is not None:
            result.append(pending_heading)
        return result

    @staticmethod
    def _split_oversized_block(block: str, limit: int) -> list[str]:
        """仅在单个语义块仍过长时，优先在句末/行末标点处切分。

        最后才使用硬上限切分超长无标点串；该退路防止恶意或损坏文档造成
        无界 chunk，同时正常 SOP 的列表、表格和完整句子会优先保持整体。
        """

        pieces: list[str] = []
        remaining = block.strip()
        while len(remaining) > limit:
            window = remaining[:limit]
            lower_bound = max(1, int(limit * 0.55))
            boundary = -1
            for index in range(len(window) - 1, lower_bound - 1, -1):
                if window[index] in _SEMANTIC_PUNCTUATION or window[index] == "\n":
                    boundary = index + 1
                    break
            if boundary == -1:
                boundary = limit
            pieces.append(remaining[:boundary].strip())
            remaining = remaining[boundary:].strip()
        if remaining:
            pieces.append(remaining)
        return pieces


__all__ = ["MarkdownChunker"]
