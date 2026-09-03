"""P1-1 STL 第二判定层与 P0-10 规则验证器的布尔一致性核对。

入口：``python -m evals.stl_consistency.harness``。它用生产 C++ 链路
（Hungarian → A*）为 P0-18 的运输类用例生成真实计划，再叠加确定性变异与
合成冲突场景，对每个计划分别跑“仅规则层”和“规则层 + STL”，逐公式比对
“公式违反 ⟺ 对应规则错误码出现”。结果写入 ``tmp/stl_consistency``。
"""
