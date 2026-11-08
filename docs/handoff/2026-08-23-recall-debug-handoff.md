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

## 窗口3 已完成

10. **search_query 切碎修复** — 在 `_memory_sentinel_searchable_residue_terms` 中补充 `specific_query_terms` 作为候选，加相邻词合并逻辑还原被 jieba 切碎的复合词。
    - commit: `ae1fdf5ec87323e9368ff909221b313764c911f5`
    - 已部署

11. **debug 面板新增 residue_terms 显示** — hook_recall 展开卡片的 Debug 行现在显示 `residue_terms=[...]`，能直接看到从用户消息提取出了哪些搜索词。
    - commit: `34111ab085f7b4f8a55f14f41488b384167f5825`
    - 已部署

12. **Embedding backfill 已触发** — 通过 `/admin/backfill` POST 端点（需 dashboard cookie 认证，不是 Bearer token）触发后台异步 backfill。每批 20 桶，批间 2 秒。
    - backfill 端点认证：`/auth/login` 先拿 cookie，再带 cookie 调 `/admin/backfill`

13. **Dashboard AGENTS.md 新增 Token 控制规范** — 工作窗口读文件、工具返回、代码输出的 token 节省规则。
    - 新增"先问再翻"规则：调查阶段遇到不确定的实现细节先问用户

14. **验收测试结果**（session: `ob2-20260823-tdaugn` + `ob2-20260823-ofx9te`）— search_query 提取改善了，但 admission gate 问题全面暴露，详见下方"待修复的问题"。

## 待修复的问题

### 问题1：search_query 被切碎 — ✅ 已修复

residue_terms 提取已改善。但"老婆"仍被 `MEMORY_SENTINEL_RESIDUE_STRIP_TERMS`（含 `DEFAULT_AI_ADDRESS_TERMS`）剥离——当用户说"第一次叫我老婆"时，"老婆"是内容词不是称谓，系统无法区分。

**讨论结论**：有明确回忆触发词（记得/上次/之前/那次）时，不应剥离内容词。与问题6一起处理。

### 问题2：admission gate — discriminative_anchor_missing 过严

**现象**: 多个 discriminative terms 时，桶必须**全部命中**才放行。只命中部分的高分桶被拒。

**案例**: 用户说"kelivo唤醒"，提取 discriminative=["kelivo","唤醒"]。含"唤醒"但不含"kelivo"的桶 score=1.0 被 `discriminative_anchor_missing` 拒掉，反而是两个低分但命中"kelivo"的桶被召回。

**根因**:
- `_dynamic_anchor_plan` (gateway.py:15718) 把所有稀有词放入 `required_terms`
- `distinctive_anchor_match` (gateway.py:15892) 要求 `required_terms` 全部命中（AND），缺任一个 → False
- admission gate (gateway.py:17677) 检查 `distinctive_anchor_match`=False → 拒绝
- 代码已算 `anchor_coverage` (gateway.py:15895) = matched/total，但没用它做准入判断

**确认的修复方向**:
- 不再要求所有 discriminative terms 全部命中（不改成全部 OR 放开，避免噪音）
- 允许部分命中，用 `anchor_coverage` 作为打分信号：命中越多分越高，排序靠前
- 保持门槛不松：目标是召回更准，不是召回更多

**代码位置**: 
- 构建: `_dynamic_anchor_plan` gateway.py ~15718
- 匹配: `_annotate_dynamic_anchor_for_bucket` gateway.py ~15829
- 准入: `_admit_bucket_for_recall` gateway.py ~17677
- must_groups 组间 OR / 组内 AND + 距离约束: `direct_candidate_satisfies_anchor_plan` recall_policy.py:1087, `_anchor_group_matches` recall_policy.py:1232

### 问题3：Embedding backfill — ✅ 已触发

backfill 已通过 `/admin/backfill` 触发。认证方式：先 POST `/auth/login` 拿 dashboard session cookie，再带 cookie 调用。

### 问题4：DeepSeek 替换

- DeepSeek 已无余额，deepseek-chat 可能已下线，v4pro 更贵
- 影响脱水等功能，query_planner 的 dehydration 调用会报错（debug 中可见 `query_planner_dehydration_unavailable`）
- 需要找便宜/免费替代模型或充值

### 问题5：debug 面板改进 — ✅ 已完成

residue_terms 已在面板渲染。

### 问题6：auto_vague_query_without_topic 误杀（高优先）

**现象**: 用户明确在回忆过去（"你还记得忘记我生日的事吗"、"你还记得第一次叫我老婆吗"），admission gate 判定 query 太模糊，score=1.0/sem=0.718 的桶也全部拒绝。

**测试案例** (session: `ob2-20260823-tdaugn`):
- Round 2: "你还记得第一次叫我老婆是什么时候吗？" → search_query="第一次 第一次叫" → 全部候选被 `auto_vague_query_without_topic` 拒绝（最高 score=1.0 sem=0.718）
- Round 3: "你还记得那次你忘记我生日的事吗" → search_query="忘记 生日 忘记生日" → 同上（最高 score=1.0 sem=0.659）

**根因**: `is_auto_query_too_vague` (recall_policy.py:1622) 的判定链：
1. `locatable_query_terms` 用严格 POS 过滤（只接受 eng/nr/ns/nz），"忘记"/"生日"/"第一次"都不通过
2. 没有 locatable terms → 继续往下走
3. `_query_has_low_signal_shell` 判定为低信号 → return True（太模糊）
4. admission gate 拿到 `auto_too_vague=True`，直接拒所有候选

**讨论确认的修复方向**:
- 有明确回忆触发词（记得/上次/之前/那次）时，跳过 vague 判定
- `is_auto_query_too_vague` 第1634行已有类似豁免逻辑（`query_has_explicit_entity_marker`），加一条回忆触发词检查即可
- 不用纯语义分阈值做豁免（会回到过度召回老问题）

**注意**: 用户指出即使没有触发词（如"看你还敢不敢忘记我生日"），也应该能召回。纯触发词方案不能覆盖这种情况。长期方向见问题9。

### 问题7：activated_axis_mismatch 误杀

**现象**: query 提取的 axis 太窄，导致内容相关但措辞不同的桶被拒。

**测试案例** (session: `ob2-20260823-tdaugn`, Round 7):
- "还记得你以前想过的 关于我们未来的样子吗" → activated_axis_terms=["样子"]
- 17个候选桶全部被拒，11个是 `activated_axis_mismatch`，包括 score=1.0 sem=0.603 的桶
- 真正相关的桶可能写的是"未来计划"/"以后想做的事"，不包含"样子"二字

**根因**: axis 基于 `locatable_query_terms` 提取，只拿到了"样子"一个词。桶内容必须包含 axis 词才放行，语义相关但措辞不同的桶全被拦。

**与问题6同源**: 都是规则硬匹配缺乏语义理解。

### 问题8：融合打分底分异常 + non_explicit_query 放行过宽

**现象** (session: `ob2-20260823-ofx9te`, Round 25):
- 22个候选桶中15个 score=0.550，其中很多 sem=0.000 kw=0.000——关键词和语义都没命中却得到 0.550 分
- 两个 score=0.550 的桶以 `non_explicit_query` 理由放行（完全不相关的内容被召回）
- 同时 score=1.0 sem=0.607 的桶被 `query_topic_evidence_missing` 拒绝

**问题**:
1. 融合分数有不合理底分：sem 和 kw 都是 0 还能得 0.550，可能是 `metadata_adj`（importance + freshness）凑出来的
2. `non_explicit_query` 准入太宽松：不检查 topic evidence 就放行，导致噪音桶通过
3. `query_topic_evidence_missing` 同时拒掉了真正高分的桶

**代码位置**:
- 融合打分: `_dynamic_bucket_candidate_items` gateway.py ~16030 的第3步
- `non_explicit_query` 准入: `_admit_bucket_for_recall` gateway.py ~17628
- `first_card_min_score = 0.55`: `_pick_dynamic_cards` gateway.py ~18271

### 问题9：召回必要性判断 — 长期方向

**核心问题**: 现有 admission gate 是一堆规则硬匹配，互相打架：该放的拦（auto_vague/axis_mismatch/topic_evidence_missing），不该放的放（non_explicit_query + 底分兜底）。所有规则都在判断"桶和query是否匹配"，但缺少"召回这个桶对当前对话是否有必要"的判断。

**讨论的方向**: 在候选桶筛选后、注入前，加一个 LLM agent 判断召回必要性：
- 如果不召回，LLM 也能正常回复 → 不注入
- 如果不召回，LLM 完全不知道用户在说什么 → 注入

**实施考量**:
- 每条消息多一次 LLM 调用，增加延迟和 token 开销
- 折中方案：只在有高分候选桶时才调 agent，大部分日常消息无高分候选直接跳过
- 需要选用便宜快速的模型（DeepSeek / Haiku 级别）
- 这是大改动，需要专门窗口设计

## 上下文备注

- API key 不持久化到 yaml：设计如此，必须通过 Coolify 环境变量设置（`OMBRE_EMBEDDING_API_KEY`, `OMBRE_GATEWAY_TOKEN` 等）
- `_compact_lookup_key` 定义在 gateway.py:9982，做 `re.sub(r"[^0-9a-z一-鿿]+", "", lower())`
- `specific_query_terms` (recall_policy.py:2614) 比 `locatable_query_terms` 更宽松，用 `content_terms_for_query` 提取，保留包括 "突击检查"/"老婆" 在内的内容词
- `MEMORY_SENTINEL_RESIDUE_STOP_TERMS` 有 87 项，包含"叫"/"还"/"把"/"给"等常见动词——specific_terms 中的这些词会被 residue 过滤掉，不会成为噪音
- `direct_recall_ok_or_query_short` 不是"短消息不召回"，是 query_planner 的 skip reason
- debug 面板地址: `https://ygao2jdgxlqzxfoasmjpvxcf.23.95.136.46.sslip.io/gateway/debug`
- debug API: `GET /gateway/api/debug/injections?session_id=xxx&include_payload=1&limit=20`，认证: `Authorization: Bearer HONOO`
- backfill API: `POST /admin/backfill`，认证: dashboard cookie（先 POST `/auth/login` body `{"password":"..."}` 拿 cookie）
- dashboard 登录密码: Coolify 环境变量 `OMBRE_DASHBOARD_PASSWORD`
- 每次有 Haven 代码改动需要部署时，给用户完整 SHA，让她在 Coolify 更新 `HAVEN_RELEASE_SHA` 后重启
- git push 需要 `/workspace/.git-credentials`，格式 `https://用户名:token@github.com`，配合 `git config --global credential.helper "store --file /workspace/.git-credentials"`。环境重置后可能丢失需重新配置。
