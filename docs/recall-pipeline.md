# Dynamic Recall Pipeline

CC 引擎 (hook_recall) 和 API 引擎 (prepare_payload) 共用底层召回逻辑，入口不同但核心路径一致。

## CC 引擎路径 (hook_recall)

```
handle_hook_recall                          gateway.py ~2340
  -> _hook_recall_fast_cards                gateway.py ~19852
       -> _route_domain_sentinel            判断是否跳过召回（技术/无关域）
       -> _build_date_recall_context        日期召回（独立通道，不走桶检索）
       -> _recall_query_plan                query planner: 判断 vague/skip
       -> _select_dynamic_buckets           核心桶选择
       -> _hook_recall_card_from_bucket     格式化输出卡片
```

## 核心桶选择

```
_select_dynamic_buckets
  -> _dynamic_bucket_candidate_items
       1. 组合有界检索源
          - BM25 keyword
          - embedding semantic
          - exact anchor / identity / relevance facet
          - Word Map / entity edge / retrieval alias
       2. 保留检索约束
          - 排除 journey、journal 与不可参与普通召回的状态/域
          - planner must terms 约束
          - session exclude_ids 硬排除
          - session semantic dedupe
       3. 生成中性候选
          - 保留 retrieval_score、keyword_score、semantic_score/status
          - 不执行旧 admission gate
          - 不执行旧 first/second-card 选卡
  -> relation-axis / planner supplemental retrieval
  -> _build_recall_shadow_debug
       - RecallNecessityPlan: none / explicit / contextual
       - 统一 relevance
       - promote / neutral / reject utility
       - score_without_freshness 排序
       - 最多选择一张
  -> _recall_shadow_with_effective_ids
       - effective_bucket_ids 即正式结果
```

## 重构规则：唯一正式召回路线

Gateway 先生成轮级 `RecallNecessityPlan`，再由统一 relevance 与 utility 产生正式结果：

- `none`：召回否定/复盘、系统测试语境或纯低信号闲聊。
- `explicit`：用户明确指向共同过去或要求搜索，并由 `targetable` 判断是否有可定位目标。
- `contextual`：没有明确回忆触发词，但当前消息含可定位自然话题；或接续指代能取得上一用户上下文。它表示允许自然召回，不保证一定注入。
- “先看/读/翻找”等自然表达不会为了使用故障降级而被统一改判为 `explicit`。

候选生成与决策已经拆开：关键词、语义、精确锚点、关系轴和 planner 补充只负责形成有界中性候选池；旧 `_admit_bucket_for_recall`、旧 `_pick_dynamic_cards` 与 semantic rescue 已退出运行路径和配置面，也没有 legacy 决策开关。Planner 的 must terms、域/状态隔离、session 硬排除和语义去重仍是候选边界。

统一 relevance 使用清理后的可信主题、语义、关键词、唯一名称/明确实体等证据。普通 keyword-only、括号动作、元数据加分或纯标题命中不能单独证明相关。称呼来自 identity 配置与 `OMBRE_RECALL_IGNORED_ADDRESS_TERMS`，只从主题证据中排除，不重算 BM25，也不改变检索拆词。

当 query 语义状态为 `query_timeout / query_failed / query_embedding_unavailable / query_embedding_failed` 且候选没有语义分时，允许两条窄范围故障降级：

- `explicit`：可信主题同时命中桶标题和实质正文，且检索阶段 `keyword_score >= 0.65`。
- `contextual`：可信主题命中实质正文，且检索阶段 `keyword_score >= 0.83`。标题命中可以作为观测证据，但不能替代正文证据。
- `disabled_for_request` 与 `indexed_not_in_semantic_top_k` 不是 query 故障，不能使用降级。

因此“先看你写的情书”保持 `contextual`；语义超时时，标题与正文都含“情书”、keyword 约 0.836 的正确桶可进入 Utility，而即使标题含“情书”且 keyword 很高，只要正文无该主题仍会拒绝。

通过 relevance 的候选进入代码版 Utility：

- 明确回忆请求与有可用前文的接续指代为 `promote`；
- 自然 `contextual` 在本地规则无法确认增量价值时为 `neutral`，仍有选择资格；
- 只有正文与当前原句完全相同等确定无增量情形才 `reject`。

选择先看 `promote`，否则看 `neutral`，按 `score_without_freshness` 排序并最多保留一张。重要度、rerank、冷却与相关性证据仍可影响检索分或排序；长期 freshness 不参与最终选卡。旧 source-record 后置追加不能绕过 Utility 或单卡上限。

`recall_shadow_debug` 是兼容沿用的字段名，不再代表影子算法或双路线对比。它记录 planner 状态、统一审核候选、Utility、未入选候选、拒绝候选和最终 `effective_bucket_ids`。候选来源使用 `direct_retrieval / relation_axis_retrieval / planner_supplemental_retrieval`，分数使用 `retrieval_score / rebuilt_score`。通过 Utility 但因 promote 优先或单卡位未选中的候选进入 `eligible_unselected_candidates`。

## 关键配置参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `recall_fusion_mode` | `"dynamic"` | 融合模式 |
| `first_card_min_score` | `0.55` | 检索融合与 moment 路径仍使用；不再作为统一桶选卡 gate |
| `second_card_min_score` | `0.50` | moment 等兼容路径仍使用；统一桶结果固定最多一张 |
| `inject_max_cards` | 2 | 动态检索/其他卡片路径的上限；统一桶结果仍固定最多一张 |
| `freshness_weight` | `0.03` | 非 dynamic 模式下时间权重 |
| `semantic_weight` | — | 非 dynamic 模式下语义权重 |
| `keyword_weight` | `0.35` | 非 dynamic 模式下关键词权重 |
| `cooldown_hours` | — | 同桶冷却时间 |
| `semantic_session_dedupe_threshold` | `0.90` | 会话语义去重阈值 |
| `embedding.query_timeout_seconds` | `5` | 单轮语义查询最多等待秒数；超时后只允许上述保守降级 |
| `_RECALL_EXCLUDED_DOMAINS` | `journey, journal` | 动态召回排除的域 |

## 候选检索与统一决策边界

`_dynamic_bucket_candidate_items` 只负责检索和硬边界，不再决定“是否注入”。它保留的约束包括：

- 普通动态桶资格、域与状态隔离；
- planner must terms；
- 调用方 `exclude_ids` 与最终输出前的 session 二次硬过滤；
- semantic session dedupe；
- 有界候选数、relation-axis 与 planner supplemental 来源标记。

`_dynamic_recall_search_query` 仍负责从自然问题提取可搜索 residue terms；`_dynamic_anchor_plan` 仍可产生检索提示和 planner 约束，但不再通过旧 admission reason 单独裁决候选。所有候选最终都由统一 relevance 与 Utility 处理。

## Debug 观测

- **Debug 面板**: `https://ygao2jdgxlqzxfoasmjpvxcf.23.95.136.46.sslip.io/gateway/debug`
- **Debug API**: `GET /gateway/api/debug/injections?session_id=xxx&include_payload=1&limit=20`，认证: `Authorization: Bearer HONOO`
- 统一决策候选记录 `candidate_origin`, `retrieval_score`, `rebuilt_score`, `semantic_score`, `semantic_status`, `retrieval_keyword_score`, relevance/utility 原因和正文主题命中。`semantic_score=null`（召回透镜显示 `—`）时看 `semantic_status`：可区分 `indexed_not_in_semantic_top_k`、embedding 缺失/过期、调用方禁用和 query timeout/failed；`—` 不等于真实语义零分。
- `recall_shadow_debug.rejected_candidates` 是统一 relevance/utility 拒绝；`eligible_unselected_candidates` 是已合格但未获唯一卡位。检索硬边界排除项可继续出现在 `suppressed_bucket_candidates`，但不参与统一审核。
- hook_recall 展开卡片显示: `search_query`, `residue_terms`, `candidates` 计数
- payload 中 `memory_sentinel_debug.searchable_residue_terms` 包含提取的搜索词列表

## 衰减 (freshness)

- `bucket_manager._calc_time_score`: `exp(-0.02 * days)`，用于 bucket_manager.search()
- gateway dynamic 模式下 freshness 只占 metadata_adjustment 的 0.02 系数，影响极小
- `bucket_manager.search()` 里 w_time=1.5（占总权重 17.6%），影响较大，但 gateway 不调这个方法
