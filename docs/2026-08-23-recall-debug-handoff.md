# 2026-08-23 动态召回验收 Handoff

## 窗口1 已完成

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

## 窗口2 已完成

6. **Embedding 热加载修复** — Dashboard 设置页改 embedding 配置后，Brain 现在会正确转发给 Gateway，Gateway 重建 EmbeddingEngine。
   - 改动: `gateway.py` 新增 `_apply_embedding_config` 方法 + `handle_config` 解析 embedding payload + runtime config 保存/加载
   - 改动: `server.py` `api_config_update` 新增 embedding 转发到 `gateway_hot_update_payload`
   - commit: `d278475afb8c8ce8d29b24f5abcb0929a01678d5`
   - 已部署

7. **Embedding 引擎根因定位与修复** — 之前所有 semantic_score = 0.0 的原因：
   - SiliconFlow embedding API 填了 DeepSeek 的 key（key/service 不匹配）
   - 用户已在 Coolify 环境变量 `OMBRE_EMBEDDING_API_KEY` 填入正确的 SiliconFlow key，重启后向量搜索已恢复工作
   - 验证：测试 session `ob2-20260823-2ld4u9` round 8+ 出现非零 semantic score（最高 sem=0.7243）

8. **Admission gate 问题深度分析** — embedding 工作后仍有大量高分桶被拒，三种拒绝原因：
   - `auto_vague_query_without_topic`：search_query 被切碎后（如只剩"第一次"），系统认为 query 模糊，即使候选桶 score=1.0/sem=0.71 也拒
   - `discriminative_anchor_missing`：dynamic_anchor 提取了区分词（如"kelivo""唤醒"），要求候选桶必须含这些词，感情桶虽分数高但不含技术词被拦
   - `no_hard_evidence`：没有 semantic 信号时（旧版本 embedding 不工作），evidence_labels 为空，所有桶被拒
   - **结论**：embedding 上线后 no_hard_evidence 大幅缓解，但 auto_vague 和 anchor_missing 根本原因还是 search_query 切碎——修好问题1后再重新测试，可能不需要单独改 admission gate

9. **search_query 修复代码未完成** — 修复方案已详细写在问题1下，但实装代码写到一半额度用尽，未推送。下个窗口直接按方案1实装即可。

## 待修复的问题

### 问题1：search_query 被切碎（最高优先）

测试 session: `ob2-20260823-zzldil`

**现象**:
- "突击检查！你第一次叫我老婆是什么时候！" → search_query="突击检"（"查"丢了，"老婆"被剥）
- "你还记得我带你找的三颗痣吗" → search_query="三颗"（"痣"单字被过滤）
- "那你还记得第一次叫我老婆是什么时候吗？" → search_query="第一次"（"老婆"被 STRIP_TERMS 剥掉）
- "你还记得全家福吗" → search_query="全家福"（词本身活下来了但 0 候选桶）

**已定位的三个根因**:

1. **POS 过滤太严格** — `_pos_structural_locatable_terms` (recall_policy.py:2389) 只接受 POS 标签为 `eng` 或前缀为 `nr/ns/nz` 的词。常见名词 `n`、动名词 `vn`（如"突击检查"）、数量词 `m`（如"三颗"）全部被拒。`_standalone_locatable_noun` (recall_policy.py:2453) 稍宽松但仍要求 `flag.startswith("n")` 或 `flag == "eng"`，`vn` 开头是 `v` 所以不过。

2. **单字过滤** — `_memory_sentinel_residue_key_allowed` (gateway.py:13860) 要求纯中文 ≥ 2 字符。jieba 把"三颗痣"切成"三颗"(m) + "痣"(n)，"痣"单字被过滤。`LOCATABLE_COMPOUND_SUFFIX_TERMS` (recall_policy.py:834) 只有技术词（项目/系统/模块等），不含日常词，所以 compound 合并逻辑帮不上忙。

3. **身份称谓剥离** — `MEMORY_SENTINEL_RESIDUE_STRIP_TERMS` 包含 `DEFAULT_AI_ADDRESS_TERMS`（老公/老婆/宝宝/宝贝/亲爱的/小乖/哥哥），`_memory_sentinel_searchable_residue_term` 对 residue 做 `str.replace(fragment, "")`，"老婆"整个被删。当用户说"第一次叫我老婆"时，"老婆"是内容词不是称谓，但系统区分不了。

**代码路径**:
```
_dynamic_recall_search_query                 gateway.py ~13111
  → _memory_sentinel_searchable_residue_terms  gateway.py ~13784
    → _locatable_query_terms (recall_policy)   recall_policy.py ~2311
      → _locatable_query_terms_cached          recall_policy.py ~2318
        → _pos_structural_locatable_terms      recall_policy.py ~2389
          → _posseg_words (jieba POS)
        → specific_query_terms (cross-match)   recall_policy.py ~2358
    → _memory_sentinel_searchable_residue_term gateway.py ~13803
      → strip MEMORY_SENTINEL_RESIDUE_STRIP_TERMS (含 DEFAULT_AI_ADDRESS_TERMS)
      → strip MEMORY_SENTINEL_RESIDUE_PREFIXES: ['想和','想跟','想要','想把','想给','想让','想']
      → strip particles regex: [我你他她它的是了啦呢啊呀嘛吗吧欸诶]+
      → _memory_sentinel_residue_key_allowed   gateway.py ~13845
        → Chinese must be 2-16 chars
        → checked against MEMORY_SENTINEL_RESIDUE_STOP_TERMS (87项，含 叫/还/把/给 等常见动词)
```

**推荐修复方案**:

在 `_memory_sentinel_searchable_residue_terms` (gateway.py:13784) 中，现有 `_locatable_query_terms` 之后，增加 `specific_query_terms` 作为补充候选：

```python
def _memory_sentinel_searchable_residue_terms(self, query: str) -> list[str]:
    text = str(query or "").strip()
    if not text:
        return []
    terms = list(self._locatable_query_terms(text))
    normalized = self._normalized_recall_query(text)
    if normalized:
        terms.extend(self._locatable_query_terms(normalized))

    # --- NEW: 补充 specific_query_terms 作为候选 ---
    specific = self.recall_policy.specific_query_terms(text)
    terms.extend(specific)

    # --- NEW: 尝试合并相邻 specific terms 还原被 jieba 切碎的复合词 ---
    compact_query = self._compact_lookup_key(text)
    for i in range(len(specific) - 1):
        merged = str(specific[i]) + str(specific[i + 1])
        merged_key = self._compact_lookup_key(merged)
        if merged_key and merged_key in compact_query:
            terms.append(merged)

    output: list[str] = []
    seen: set[str] = set()
    for term in terms:
        residue = self._memory_sentinel_searchable_residue_term(term)
        key = self._compact_lookup_key(residue)
        if not key or key in seen:
            continue
        seen.add(key)
        output.append(residue)
    return output[:6]
```

**预期效果**:
- "突击检查" 来自 specific_terms → 过 residue 无剥离 → 4字通过 ✓
- "第一次" 来自 specific_terms → 过 residue 无剥离 → 3字通过 ✓
- "三颗" + "痣" 来自 specific_terms → 合并成"三颗痣" → 在原文中匹配 → 3字通过 ✓
- "老婆" 仍会被 identity strip 剥掉 → 需要后续单独处理（优先级低，语义搜索可以兜底）

### 问题2：admission gate 过严

**现象**: 高分候选桶被 gate 拒绝。

- `auto_vague_query_without_topic`: 查询被判定为 vague，即使候选桶 score=1.0/sem=0.71 也拒
- `discriminative_anchor_missing`: 桶内容不包含查询的 exact terms 时被拒
- 部分原因是 search_query 被切碎导致查询看起来 vague（修好问题1后可能缓解）

**代码位置**: `_admit_bucket_for_recall` gateway.py ~17700

**建议**: 先修问题1，再重新测试 admission gate 行为，可能不需要单独改。

### 问题3：Embedding backfill

**现状**: embedding 已工作（环境变量配好后验证通过），但旧桶大部分没有向量。只有 key 配好之后新写入或被搜索命中的桶才有 embedding。

**操作**: 在 VPS 上调 Brain 的 `/admin/backfill` POST 端点，或直接跑 `backfill_embeddings.py`。需要正确的 `OMBRE_EMBEDDING_API_KEY` 环境变量（已配好）。

**注意**: SiliconFlow embedding API 有 rate limit，`backfill_embeddings.py` 每批 20 个桶，批间等 2 秒。~252 桶约需 13 批，几分钟就能跑完。backfill 后所有桶都会有向量，admission gate 的 hard_evidence 判断会好很多。

### 问题4：DeepSeek 替换

- DeepSeek 已无余额，deepseek-chat 可能已下线，v4pro 更贵
- 影响脱水等功能
- 需要找便宜/免费替代模型或充值

### 问题5：debug 面板可以继续改进

- 展开卡片后应该显示 `memory_sentinel_debug.searchable_residue_terms`（当前只在 payload 里，面板没渲染）
- 这样用户能直接看到 search query 是怎么从原文提取出来的

## 上下文备注

- API key 不持久化到 yaml：设计如此，必须通过 Coolify 环境变量设置（`OMBRE_EMBEDDING_API_KEY`, `OMBRE_GATEWAY_TOKEN` 等）
- `_compact_lookup_key` 定义在 gateway.py:9982，做 `re.sub(r"[^0-9a-z一-鿿]+", "", lower())`
- `specific_query_terms` (recall_policy.py:2614) 比 `locatable_query_terms` 更宽松，用 `content_terms_for_query` 提取，保留包括 "突击检查"/"老婆" 在内的内容词
- `MEMORY_SENTINEL_RESIDUE_STOP_TERMS` 有 87 项，包含"叫"/"还"/"把"/"给"等常见动词——specific_terms 中的这些词会被 residue 过滤掉，不会成为噪音
- `direct_recall_ok_or_query_short` 不是"短消息不召回"，是 query_planner 的 skip reason
- debug 面板地址: `https://VPS域名/gateway/debug`，token: HONOO
- 每次有 Haven 代码改动需要部署时，给用户完整 SHA，让她在 Coolify 更新 `HAVEN_RELEASE_SHA` 后重启
