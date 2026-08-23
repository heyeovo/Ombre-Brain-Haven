# 2026-08-23 动态召回验收 Handoff

## 本窗口已完成

1. **hook_recall 写 injection_debug** — CC引擎(hook_recall)路径现在会写入 injection_debug 记录，debug面板能看到数据了。
   - commit: `c46f677`

2. **journey/journal 域排除动态召回** — 统一用 `_RECALL_EXCLUDED_DOMAINS` frozenset，四个候选函数 (`_is_dynamic_candidate`, `_is_semantic_candidate_bucket`, `_is_identity_name_candidate_bucket`, `_is_relevance_candidate_bucket`) 都调同一个检查。以后加新域只改一处。
   - commit: `e80a6a9`

3. **debug 面板适配 hook_recall 格式** — 面板现在能正确显示 hook_recall 的 recalled/suppressed 桶、分数、admission_reason、search_query、domain sentinel 等。
   - commit: `3700217`

4. **recall-pipeline.md** — 动态召回架构文档，新窗口先读这个再进代码。
   - 位置: `docs/recall-pipeline.md`
   - AGENTS.md 已加指路

5. **已部署**: SHA `3700217d8a675b6a38cc527795631610827a8e7b`

## 待修复的问题

### 问题1：search_query 被切碎（最高优先）

测试 session: `ob2-20260823-zzldil`

**现象**:
- "老婆突击检查" → search_query="突击检"（"老婆"被STRIP_TERMS剥掉没问题，但"查"被丢了）
- "你还记得三颗痣吗" → search_query="当时 三颗"（"痣"丢了，且混入了原文没有的"当时"）
- "你还记得全家福吗" → 0候选桶（jieba可能把"全家福"切成"全家"+"福"，"福"单字被过滤）

**根因**: `_memory_sentinel_searchable_residue_terms` 依赖 jieba 分词 → POS 过滤 → residue 剥离三层处理，对专有名词和短句的切分不稳定。

**代码路径**:
```
_hook_recall_fast_cards
  → _dynamic_recall_search_query        gateway.py ~13084
    → _memory_sentinel_searchable_residue_terms  gateway.py ~13757
      → recall_policy.locatable_query_terms      recall_policy.py ~2311
        → _posseg_words (jieba)                  recall_policy.py ~2229
        → _pos_structural_locatable_terms        recall_policy.py ~2389
      → _memory_sentinel_searchable_residue_term gateway.py ~13776
```

**建议修复方向**:
- 方案A（保守）：在 `_memory_sentinel_searchable_residue_terms` 最前面，把原始 query 做一次整体 residue 处理后直接加入候选 terms（不经过 jieba 切分），保证原始关键词不被切碎。jieba 的结果作为补充。
- 方案B（激进）：简化 search_query 提取逻辑，减少过滤层数。

### 问题2：两个窗口同一消息不同召回结果

**原因已确认**: 不是上下文影响（CC引擎只传当前消息作为 query），是 session_id 不同导致 cooldown 和 semantic_session_dedupe 状态不同。同一个桶在一个窗口刚被召回过就会冷却。这是预期行为，不需要修。

### 问题3：衰减降权

**现状**:
- gateway 动态路径（CC引擎用的）: freshness 只占 metadata_adjustment 的 0.02 系数，影响极小
- bucket_manager.search()（API引擎用的）: w_time=1.5 占总权重 17.6%，影响较大
- 衰减公式: `exp(-0.02 * days)`，150天前的桶只有 0.05 分

**用户倾向**: 不做衰减。认为衰减限制了 AI 记忆的优势。

**建议**: gateway 路径已经几乎没衰减了。如果要改 bucket_manager.search() 的衰减，把 `w_time` 设为 0 或极小值即可（`bucket_manager.py:111`）。但 gateway 路径不调 `bucket_manager.search()`，所以这个改动主要影响 API 引擎和 MCP 工具（breath 等）。

### 问题4：debug 面板可以继续改进

- 展开卡片后应该显示 `memory_sentinel_debug.searchable_residue_terms`（当前只在 payload 里，面板没渲染）
- 这样用户能直接看到 search query 是怎么从原文提取出来的

## 上下文备注

- `direct_recall_ok_or_query_short` 不是"短消息不召回"，是 query_planner 的 skip reason，意思是"直接召回阶段已经有结果（或 query 太短不需要 planner 补充搜索）"，不影响直接召回本身
- debug 面板地址: `https://VPS域名/gateway/debug`，token: HONOO
- 每次有 Haven 代码改动需要部署时，给用户完整 SHA，让她在 Coolify 更新 `HAVEN_RELEASE_SHA` 后重启
