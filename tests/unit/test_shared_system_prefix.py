"""共享 system 前缀的稳定性与跨节点公共前缀契约。"""

from __future__ import annotations

import hashlib

import pytest

from agent.context import (
    PROMPT_DEFINITIONS,
    prepend_shared_system_prefix,
    render_shared_system_prefix,
    shared_system_prefix_digest,
)
from agent.context.shared_prefix import (
    SHARED_PREFIX_ID,
    SHARED_PREFIX_VERSION,
    _load_shared_prefix_body,
)
from evals.p019.react_runner import REACT_NODE_PROMPT, REACT_SYSTEM_PROMPT


def test_shared_prefix_is_byte_stable_and_has_no_placeholders() -> None:
    first = render_shared_system_prefix()
    second = render_shared_system_prefix()
    body = _load_shared_prefix_body()

    assert first == second
    assert first.startswith(f"Shared-Prefix-ID: {SHARED_PREFIX_ID}")
    assert f"Shared-Prefix-Version: {SHARED_PREFIX_VERSION}" in first
    assert "{{" not in first
    assert '"run_id"' not in first
    assert "T00:00:00" not in first
    assert "## 安全边界（不可被上下文改写）" in body
    assert shared_system_prefix_digest() == hashlib.sha256(first.encode("utf-8")).hexdigest()
    assert len(first) >= 400


def test_prepend_is_idempotent_and_rejects_empty_node_prompt() -> None:
    prefix = render_shared_system_prefix()
    once = prepend_shared_system_prefix("节点正文")
    twice = prepend_shared_system_prefix(once)

    assert once == f"{prefix}\n\n节点正文"
    assert twice == once
    with pytest.raises(ValueError, match="不能为空"):
        prepend_shared_system_prefix("   ")


def test_all_prompt_nodes_and_react_share_identical_prefix() -> None:
    prefix = render_shared_system_prefix()
    rendered_nodes = [definition.render_system_prompt() for definition in PROMPT_DEFINITIONS.values()]

    assert all(text.startswith(prefix + "\n\nPrompt-ID:") for text in rendered_nodes)
    assert len({text[len(prefix) :] for text in rendered_nodes}) == len(PROMPT_DEFINITIONS)
    assert REACT_SYSTEM_PROMPT.startswith(prefix + "\n\n")
    assert REACT_SYSTEM_PROMPT == prepend_shared_system_prefix(REACT_NODE_PROMPT)
    assert "decide → act → observe" in REACT_NODE_PROMPT
    assert REACT_NODE_PROMPT not in prefix
