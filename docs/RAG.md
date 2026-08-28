# P0-07 仓储 SOP RAG

## 1. 固定语料与数据流

知识库位于 `domains/amr_warehouse/knowledge/`，当前固定 6 份 UTF-8 Markdown。每份文件必须带 YAML Front Matter：

- `doc_id`
- `title`
- `version`
- `role_scope`
- `source`
- `status`

Loader 使用 `yaml.safe_load()`，以原始文件字节计算 SHA-256，只接纳 `status=frozen`。完整索引链路为：

```text
Markdown + Front Matter
  → frozen/UTF-8/checksum 校验
  → DocumentService 幂等同步并从 PostgreSQL 回读正文
  → Markdown ## section-aware chunking
  → Qwen3-Embedding-0.6B 文档向量
  → Qdrant dense cosine points + 完整 payload
  → documents.status=indexed / indexed_at（仅在 Qdrant 成功后）
```

当前模型固定路径为 `E:\Llama.cpp\Embedding`。维度由实际 SentenceTransformer 模型动态读取；2026-08-20 实测为 1024，代码和配置都没有写死该数字。

`RAG 示例问题` section 仍保留在冻结源文档中，但不生成检索 chunk。该 section 只有问题清单，没有规则答案；把它当成证据会导致问题词面命中自身并抬高无答案分数。

## 2. 分块与引用契约

优先按精确 Markdown `##` 切 section；`###` 及更低级标题留在父 section。只有完整 section 超过 `chunk_max_chars` 时，才按空行形成的段落、列表、表格和低级标题语义块二次打包；单个块仍超限时优先在句末、行末或标点处拆分。

每个 `KnowledgeChunk` 至少包含：

```text
chunk_id / doc_id / title / section / version / role_scope /
source / checksum / text
```

`chunk_id` 由文档 ID、section/part 序号和正文摘要确定性生成；Qdrant point ID 再由 collection 名和 chunk ID 生成 UUIDv5。相同输入重复索引不会产生重复 points。

`RetrievalResult` 返回上述来源字段，以及 `hybrid_score`、原始 `vector_score`、原始 `bm25_score`、两个归一化分数和稳定 citation。通过证据门禁的结果可转换为 P0-05 `ContextEvidence(source_type="rag")`；`source_id` 使用 chunk ID，以允许同一文档的多个 section 同时进入有限上下文。

公共 Schema：

- `docs/schemas/KnowledgeChunk.schema.json`
- `docs/schemas/RetrievalResult.schema.json`
- `docs/schemas/RetrievalResponse.schema.json`

## 3. ACL 执行位置

ACL 必须先于候选返回：

- Qdrant：`role_scope` 建 keyword payload index，每次 `query_points()` 都带 `query_filter`。可选 `document_ids` 也在同一 filter 中执行。
- BM25：先按 `role_scope` 和可选 `document_ids` 过滤 chunks，再把允许语料交给 `BM25Okapi` 评分。
- Hybrid：融合前再做防御性断言；该断言是泄漏熔断，不替代前两处过滤。

`viewer` 只能匹配 payload 中包含 `viewer` 的 chunk；`operator` 匹配包含 `operator` 的 chunk。`amr_fault_handling_v1_0` 与 `dispatch_approval_policy_v1_0` 只有 `operator`，不能进入 viewer 的 vector 或 BM25 候选。文档正文中的自然语言不能提升角色。

## 4. Hybrid 分数与拒答

P0 不实现 Reranker。两路候选归一化后直接融合：

```text
normalized_vector = (clip(cosine, -1, 1) + 1) / 2
normalized_bm25   = 1 - exp(-max(raw_bm25, 0) / bm25_saturation)
hybrid            = vector_weight * normalized_vector
                  + bm25_weight * normalized_bm25
```

默认权重为 `0.5 / 0.5`，`bm25_saturation=3.0`，均可配置。拒答使用两个可配置的绝对证据门禁：

```text
top.hybrid_score >= 0.809
OR
top.vector_score >= 0.499
```

若两者都未达到，响应为 `insufficient_evidence`，`results=[]`；弱候选正文不会进入后续 Agent。

阈值来自真实评测，不是模型名推断值。移除非证据 section 后，初始问题集的可答最低 hybrid 为 0.821220，不可答最高为 0.797038，中点约 0.809。加入更短的语义改写“AMR 当前电量 25% 属于哪个区间？”后，hybrid 单阈值不再可分；在 hybrid 未达标子集中，该可答问题 top vector 为 0.597388，3 个不可答问题为 0.320516～0.400358，中点为 0.498873，因此 vector 补充门禁取 0.499。

阈值只允许从 `evals/rag/cases.json` 的 calibration 子集建议；对外发布的 Recall/MRR/
Precision/nDCG/citation/answerability/ACL 只在 test+attack holdout 上计算。CLI 在默认发布指标失败或
`citation_total=0` 时必须非零退出。2026-08-20 的 20 例同集数字（MRR=0.970588 等）
仅作为历史基线，不能再当作发布 holdout 指标。

## 5. 运行命令

先确认 PostgreSQL/Qdrant 已运行且数据库 revision 正确：

```powershell
docker compose up -d postgres qdrant
& 'E:\Anaconda\envs\torch128\python.exe' .\scripts\migrate_database.py check
```

重建正式索引：

```powershell
$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'
& 'E:\Anaconda\envs\torch128\python.exe' .\scripts\index_warehouse_knowledge.py
```

不删除 collection、只替换本批 doc ID 的旧 points：

```powershell
& 'E:\Anaconda\envs\torch128\python.exe' .\scripts\index_warehouse_knowledge.py --no-rebuild
```

执行一次查询：

```powershell
& 'E:\Anaconda\envs\torch128\python.exe' .\scripts\query_warehouse_knowledge.py `
  'AMR 当前电量 25% 属于哪个区间？' --role viewer --top-k 5
```

运行固定 20 例评测：

```powershell
& 'E:\Anaconda\envs\torch128\python.exe' -m evals.rag.run_eval `
  --output .\tmp\p007_rag_eval.json
```

需要从源文档开始复现时增加 `--rebuild-index`。评测报告包含每例候选、原始/融合分数、answerable/unanswerable 分布、建议阈值、Recall@K、MRR、section recall、Precision@K、nDCG@K、Citation Correctness、answerability accuracy 和 ACL leak 明细。

Precision@K 与 nDCG@K 默认作为诊断指标输出。由于它们是在本次需求前没有预先冻结门槛的新增指标，
不能读取 holdout 分数后再反向设置默认阈值；如需将其纳入某次独立验收，可显式传入
`--min-precision-at-k` 与 `--min-ndcg-at-k`，门禁不达标时同样以退出码 2 失败。

## 6. 指标定义与当前实测

- Recall@K：每个可答 case 的期望文档在 Top-K 中的召回比例，再对 case 求平均。
- MRR：每个可答 case 第一个期望文档的 reciprocal rank，再求平均。
- Section Recall@K：期望 `(doc_id, section)` 在 Top-K 中的召回比例。
- Precision@K：以唯一 `(doc_id, section)` 为相关单元，相关章节第一次命中记 1，重复 chunk 记 0；
  每例除以其冻结的 K，再对可答 case 宏平均。候选不足 K 时尾部按 0 处理。
- nDCG@K：沿用同一章节级二元增益，按 `1/log2(rank+1)` 折损后除以理想 DCG；不可答 case 因没有
  相关性 oracle，不参与 Precision 或 nDCG。
- Citation Correctness：公开响应中的 citation 是否逐字段回指当前源 chunk 的 doc/version/section/checksum/text。
- ACL leak count：阈值前全部候选中，角色不匹配或命中 case 禁止文档的次数。

2026-08-28 使用真实 Qwen3-Embedding-0.6B + Qdrant/BM25 执行
`python -m evals.rag.run_eval --output tmp\p020_release_rag_eval_rank_metrics.json`。固定 20 例中
8 例只用于校准，以下发布结果只统计 8 test + 4 attack holdout，其中 11 例可回答、1 例应拒答：

| 指标 | 结果 |
|---|---:|
| published cases | 12 |
| Recall@K | 1.000000 |
| MRR | 1.000000 |
| Section Recall@K | 1.000000 |
| Precision@K | 0.236364 |
| nDCG@K | 1.000000 |
| Citation Correctness | 1.000000（58/58） |
| Answerability Accuracy | 1.000000 |
| ACL leak count | 0 |

Precision@K 的宏平均由 8 个 `1/5` 和 3 个 `2/6` 组成。oracle 只标注回答所必需的章节，未标注候选
统一按非相关处理，因此它衡量的是必需证据密度，不是对其余候选语义价值的完整人工判定。nDCG@K=1
表示这些已标注章节均排在理想位置。该规模是当前冻结语料的 P0 holdout 基线，不代表开放域统计结论；
2026-08-20 的 17 可答 + 3 不可答同集结果继续只作为历史记录。
