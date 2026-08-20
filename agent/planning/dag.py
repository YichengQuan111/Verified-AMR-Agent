"""不依赖第三方图算法的确定性 DAG 校验。"""

from __future__ import annotations

import heapq
from collections.abc import Iterable, Mapping


class DAGValidationError(ValueError):
    """依赖图引用未知节点、重复依赖、自依赖或形成循环。"""


def topological_sort(dependencies: Mapping[str, Iterable[str]]) -> list[str]:
    """使用 Kahn 算法校验依赖图，并返回稳定的拓扑顺序。

    ``dependencies`` 的键是节点 ID，值是该节点必须等待的前置节点 ID。算法先把所有
    入度为 0 的节点放入最小堆，每取出一个节点就删除它指向后继节点的边。若最终取出
    的节点数少于总节点数，剩余节点之间必然存在循环依赖。
    """

    node_ids = set(dependencies)
    normalized: dict[str, list[str]] = {}

    for node_id, raw_dependencies in dependencies.items():
        dependency_ids = list(raw_dependencies)
        if len(dependency_ids) != len(set(dependency_ids)):
            raise DAGValidationError(f"节点 {node_id} 包含重复依赖")
        if node_id in dependency_ids:
            raise DAGValidationError(f"节点 {node_id} 不能依赖自身")

        unknown = sorted(set(dependency_ids) - node_ids)
        if unknown:
            raise DAGValidationError(
                f"节点 {node_id} 引用了未知依赖: {', '.join(unknown)}"
            )
        normalized[node_id] = dependency_ids

    # indegree 表示每个节点尚未完成的前置任务数；dependents 保存反向邻接表，
    # 便于一个前置任务完成后，把所有直接后继的入度减一。
    indegree = {node_id: len(items) for node_id, items in normalized.items()}
    dependents: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    for node_id, dependency_ids in normalized.items():
        for dependency_id in dependency_ids:
            dependents[dependency_id].append(node_id)

    ready = [node_id for node_id, degree in indegree.items() if degree == 0]
    heapq.heapify(ready)
    ordered: list[str] = []

    while ready:
        node_id = heapq.heappop(ready)
        ordered.append(node_id)
        for dependent_id in sorted(dependents[node_id]):
            indegree[dependent_id] -= 1
            if indegree[dependent_id] == 0:
                heapq.heappush(ready, dependent_id)

    if len(ordered) != len(node_ids):
        cyclic_nodes = sorted(
            node_id for node_id, degree in indegree.items() if degree > 0
        )
        raise DAGValidationError(
            f"依赖图存在循环，涉及节点: {', '.join(cyclic_nodes)}"
        )
    return ordered


def validate_dag(dependencies: Mapping[str, Iterable[str]]) -> None:
    """只执行 DAG 校验；调用方不需要拓扑顺序时使用此入口。"""

    topological_sort(dependencies)


__all__ = ["DAGValidationError", "topological_sort", "validate_dag"]
