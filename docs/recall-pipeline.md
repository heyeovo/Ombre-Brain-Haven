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

## Admission Gate 详细路径

```
_admit_bucket_for_recall                    gateway.py ~17628
  -> _bucket_evidence_labels                证据标签（distinctive_anchor, strong_semantic 等）
  -> _anchor_plan_direct_rejection          锚点硬拒（must_groups 全 AND）
  -> recall_policy.assess                   recall_policy.py ~2763
       -> auto_too_vague 判定               来自 is_auto_query_too_vague (recall_policy.py:1622)
       -> topic_evidence 检查
  -> discriminative_anchor_missing 检查     gateway.py ~17677
  -> non_explicit_query 准入               （较宽松，不检查 topic evidence）
  -> activated_axis_mismatch               桶不在 query 的 activated axis 上
  -> semantic_only                         只有语义信号没有关键词信号
  -> query_topic_evidence_missing          桶不包含 query 的 topic 证据词
  -> relationship_background_without_query_topic_evidence
```

### auto_vague 判定链 (is_auto_query_too_vague)

```
is_auto_query_too_vague                     recall_policy.py ~1622
  -> _is_reaction_only_query               纯反应（嗯/哦/好的）→ vague
  -> _is_probe_only_query                  纯探测 → vague
  -> _is_short_casual_only_query           短闲聊 → vague
  -> query_has_explicit_entity_marker      有实体标记 → NOT vague
  -> _query_has_relative_date_recall_hint  有相对日期 → NOT vague
  -> _is_affection_only_query              纯情感 → vague
  -> is_detail_read_query                  详细阅读 → NOT vague
  -> locatable_query_terms                 有可定位词 → NOT vague（POS 过滤严格，常见词过不了）
  -> is_emotional_reason_lookup            情感原因查询 → NOT vague
  -> _query_has_low_signal_shell           低信号壳 → vague
  -> AUTO_VAGUE_RECALL_MARKERS check       最终兜底
```

### search_query 提取链

```
_dynamic_recall_search_query                gateway.py ~13111
  → _memory_sentinel_searchable_residue_terms  gateway.py ~13784
    → _locatable_query_terms (recall_policy)   recall_policy.py ~2311（POS 严格：只 eng/nr/ns/nz）
    → specific_query_terms (recall_policy)     recall_policy.py ~2614（较宽松，content_terms_for_query）
    → 相邻词合并                                还原被 jieba 切碎的复合词
    → _memory_sentinel_searchable_residue_term gateway.py ~13803
      → strip MEMORY_SENTINEL_RESIDUE_STRIP_TERMS（含 DEFAULT_AI_ADDRESS_TERMS: 老公/老婆/宝宝/宝贝/…）
      → strip MEMORY_SENTINEL_RESIDUE_PREFIXES: [想和/想跟/想要/想把/想给/想让/想]
      → strip particles regex: [我你他她它的是了啦呢啊呀嘛吗吧欸诶]+
      → _memory_sentinel_residue_key_allowed   gateway.py ~13845（中文 ≥2 字符，87 项停用词过滤）
```

### discriminative anchor 判定链

```
_dynamic_anchor_plan                        gateway.py ~15718
  → _dynamic_anchor_query_terms             提取 query terms
  → lexical_term_specificity_stats          计算每个 term 在桶库中的出现频率
  → 分类: discriminative（稀有）/ category（常见）/ support（辅助）/ unseen
  → required_terms = discriminative_terms

_annotate_dynamic_anchor_for_bucket         gateway.py ~15829
  → covered(term) 检查桶的 name/subject/keywords/tags/full_text 是否包含 term
  → distinctive_anchor_match = bool(required_terms and not missing_terms)  # 全 AND
  → anchor_coverage = len(matched) / len(discriminative)                   # 已算但未用于准入

_admit_bucket_for_recall                    gateway.py ~17677
  → dynamic_anchor_missing = required_terms AND NOT distinctive_anchor_match
  → 如果 missing 且没有独立 anchor evidence → 拒绝 "discriminative_anchor_missing"

must_groups (recall_policy.py:1087):
  → 组间: any() = OR
  → 组内: _anchor_group_matches (recall_policy.py:1232) = 全 AND + 距离约束 ANCHOR_MUST_GROUP_MAX_SPAN
```

## Debug 观测

- **Debug 面板**: `https://ygao2jdgxlqzxfoasmjpvxcf.23.95.136.46.sslip.io/gateway/debug`
- **Debug API**: `GET /gateway/api/debug/injections?session_id=xxx&include_payload=1&limit=20`，认证: `Authorization: Bearer HONOO`
- 每个候选桶的 debug 包含: `score`, `semantic_score`, `keyword_score`, `admission_reason`, `evidence_labels`
- 被拒绝的桶在 `suppressed_bucket_candidates` 里，带 `admission_reason`
- hook_recall 展开卡片显示: `search_query`, `residue_terms`, `candidates` 计数
- payload 中 `memory_sentinel_debug.searchable_residue_terms` 包含提取的搜索词列表

## 衰减 (freshness)

- `bucket_manager._calc_time_score`: `exp(-0.02 * days)`，用于 bucket_manager.search()
- gateway dynamic 模式下 freshness 只占 metadata_adjustment 的 0.02 系数，影响极小
- `bucket_manager.search()` 里 w_time=1.5（占总权重 17.6%），影响较大，但 gateway 不调这个方法
