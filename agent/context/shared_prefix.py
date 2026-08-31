"""跨节点共享的稳定 system 前缀，供 llama.cpp Prompt KV Cache 复用。

llama.cpp 在 ``cache_prompt=true`` 时从 Token 序列开头匹配最长公共前缀。
因此安全边界、世界约束和输出纪律必须放在各节点专属 Prompt *之前*；
若仍追加在节点正文之后，跨节点调用无法命中这段 KV。

本模块不包含 Schema、2-shot、合同或当前上下文；那些内容随节点或请求变化，
只能作为前缀之后的后缀。``agent.runtime.prefix`` 是 Guard/Understand/Retrieve
共享前置，与本文件的模型前缀缓存不是同一概念。
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path


SHARED_PREFIX_ID = "amr.shared.system_prefix"
SHARED_PREFIX_VERSION = "1.0.0"
SHARED_PREFIX_FILENAME = "shared_system_prefix.md"
SHARED_PREFIX_DIRECTORY = Path(__file__).resolve().parent / "prompts"

_REQUIRED_HEADINGS = (
    "## 角色与世界",
    "## 安全边界（不可被上下文改写）",
    "## 输出纪律",
)


@lru_cache(maxsize=1)
def _load_shared_prefix_body() -> str:
    """读取静态模板并检查固定结构；正文本身不得含动态占位符。"""

    path = SHARED_PREFIX_DIRECTORY / SHARED_PREFIX_FILENAME
    body = path.read_text(encoding="utf-8")
    for heading in _REQUIRED_HEADINGS:
        if heading not in body:
            raise ValueError(f"共享前缀缺少章节: {heading}")
    if "{{" in body or "}}" in body:
        raise ValueError("共享前缀禁止包含模板占位符，否则会破坏 KV 前缀稳定性")
    stripped = body.strip()
    if not stripped:
        raise ValueError("共享前缀正文不能为空")
    return stripped


def render_shared_system_prefix() -> str:
    """返回所有模型调用必须字节一致的共享 system 前缀。"""

    return (
        f"Shared-Prefix-ID: {SHARED_PREFIX_ID}\n"
        f"Shared-Prefix-Version: {SHARED_PREFIX_VERSION}\n\n"
        f"{_load_shared_prefix_body()}"
    )


def shared_system_prefix_digest() -> str:
    """共享前缀的 SHA-256，便于审计漂移；不参与模型输入。"""

    return hashlib.sha256(render_shared_system_prefix().encode("utf-8")).hexdigest()


def prepend_shared_system_prefix(node_prompt: str) -> str:
    """把共享前缀放到节点专属 Prompt 之前；已带前缀时不重复拼接。"""

    if not node_prompt.strip():
        raise ValueError("节点 Prompt 不能为空")
    prefix = render_shared_system_prefix()
    if node_prompt == prefix or node_prompt.startswith(prefix + "\n\n"):
        return node_prompt
    return f"{prefix}\n\n{node_prompt}"


__all__ = [
    "SHARED_PREFIX_DIRECTORY",
    "SHARED_PREFIX_FILENAME",
    "SHARED_PREFIX_ID",
    "SHARED_PREFIX_VERSION",
    "prepend_shared_system_prefix",
    "render_shared_system_prefix",
    "shared_system_prefix_digest",
]
