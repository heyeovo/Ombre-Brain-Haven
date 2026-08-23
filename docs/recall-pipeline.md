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
_select_dynamic_buckets                     gateway.py ~16509
  -> _dynamic_bucket_candidate_items        gateway.py ~16030
       1. 候选池构建 (eligible filter)
          - _is_dynamic_candidate           普通桶资格（排除 feel/permanent/archived/resolved/pinned）
          - _is_semantic_candidate_bucket   语义池资格（更宽松）
          - _is_identity_name_candidate_bucket  身份名匹配
          - _is_relevance_candidate_bucket  相关性 facet 匹配
          - _RECALL_EXCLUDED_DOMAINS        统一域排除（journey, journal）
          - _is_relevance_suppressed        相关性抑制

       2. 信号收集
          - _get_keyword_candidates         BM25 关键词匹配
          - _get_semantic_candidates        向量语义匹配
          - _get_exact_anchor_candidates    精确锚点匹配
          - _get_word_map_hint_scores       词图提示分
          - _get_entity_edge_boosts         实体边提升
          - _retrieval_alias_hits           检索别名命中
          - _dynamic_anchor_plan            动态锚点计划

       3. 融合打分 (recall_fusion_mode="dynamic")
          fusion = alpha * vector_norm + (1-alpha) * keyword_norm
          metadata_adj = 0.02 * importance + 0.02 * freshness
          final = clamp(fusion + word_map_adj + metadata_adj - cooldown_penalty)

       4. 准入门槛
          - _admit_bucket_for_recall        gateway.py ~17628
            -> _bucket_evidence_labels      证据标签
            -> _anchor_plan_direct_rejection 锚点拒绝
            -> recall_policy.assess         策略评估
            -> 各种 hard evidence 检查
          - admission_reason 记录拒绝原因

       5. 会话去重
          - _filter_semantic_session_deduped_bucket_items

  -> _pick_dynamic_cards                    gateway.py ~18271
       - first_card_min_score = 0.55
       - second_card_min_score = 0.50
       - _dynamic_bucket_item_has_reliable_recall_signal 可绕过分数阈值
       - 轴多样性选择 (_pick_axis_diverse_dynamic_cards)

  -> _try_semantic_rescue                   语义救援（被抑制的桶二次机会）
```

## 关键配置参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `recall_fusion_mode` | `"dynamic"` | 融合模式 |
| `first_card_min_score` | `0.55` | 第一张卡最低分 |
| `second_card_min_score` | `0.50` | 第二张卡最低分 |
| `inject_max_cards` | 2 | 最多注入几张卡 |
| `freshness_weight` | `0.03` | 非 dynamic 模式下时间权重 |
| `semantic_weight` | — | 非 dynamic 模式下语义权重 |
| `keyword_weight` | `0.35` | 非 dynamic 模式下关键词权重 |
| `cooldown_hours` | — | 同桶冷却时间 |
| `semantic_session_dedupe_threshold` | `0.90` | 会话语义去重阈值 |
| `_RECALL_EXCLUDED_DOMAINS` | `journey, journal` | 动态召回排除的域 |

## Debug 观测

- **Debug 面板**: `/gateway/debug` — 查看每轮的 injection_debug 记录
- 每个候选桶的 debug 包含: `score`, `semantic_score`, `keyword_score`, `admission_reason`, `evidence_labels`
- 被拒绝的桶在 `suppressed_bucket_candidates` 里，带 `admission_reason`

## 衰减 (freshness)

- `bucket_manager._calc_time_score`: `exp(-0.02 * days)`，用于 bucket_manager.search()
- gateway dynamic 模式下 freshness 只占 metadata_adjustment 的 0.02 系数，影响极小
- `bucket_manager.search()` 里 w_time=1.5（占总权重 17.6%），影响较大，但 gateway 不调这个方法
