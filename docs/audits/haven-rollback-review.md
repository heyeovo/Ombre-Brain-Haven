# Haven 疑似回退件逐项复核

对应 OB Todo：`e006864b`。基线：`12a1577`；分支：`chore/rollback-review`。审计旧快照的行号仅用于识别符号；以下删除项位置为修改前工作副本位置，其余为最终位置。

每项已执行排除 .venv/.git 的 `rg -n -w <符号> .`，读取定义上下文，并检查 `git log -S<符号> --oneline | tail -3`。引用中的注释、文档、文件名不视为调用。TECH_DEBT 无卡点名这些符号；MEM-01 保留每日审核入口，不直接证明这些 helper 为回退件。

分类：A 有效调用；B 明确保留且已在定义上方注明；C 删除；D 证据不足，保持原样。

| 符号 | 当前位置（C 为原位置） | 分类 | 一句话依据 |
|---|---|---|---|
| `record_surface_trace` | `bucket_manager.py:1924` | D | 文档仍声明 surface trace，但未找到调用；需确认该观测能力是否废弃。 |
| `continue_context` | `darkroom.py:239` | D | 仅定义；房间 continue 输出仍完整，历史未明确此入口的保留/废弃契约。 |
| `DIGEST_PROMPT` | `dehydrator.py:182` | C | 运行时 _api_digest 按 self.identity 渲染 DIGEST_PROMPT_TEMPLATE，旧静态值无开关。 |
| `MERGE_PROMPT` | `dehydrator.py:207` | C | _api_merge 使用产品 prompt 与按身份渲染的硬约束，旧静态值无开关。 |
| `_api_dehydrate` | `dehydrator.py:396` | C | dehydrate 明确停用 LLM JSON 压缩，改为原文格式化；胶囊有独立 API。 |
| `_normalize_model_result` | `dream_engine.py:722` | C | generate 使用纯文本梦、材料情绪与本地 cues；旧 JSON 结果规范化无调用。 |
| `SEMANTIC_RESCUE_SYSTEM_PROMPT` | `gateway.py:157` | C | 6f1e317 删除 semantic rescue 调用；召回文档明确没有 legacy 开关。 |
| `_update_persona_after_response` | `gateway.py:6357` | D | 仅定义；回复后 Persona 更新仍有其他入口，旧 HTTP response 适配是否需恢复不明。 |
| `_query_has_handoff_transition_marker` | `gateway.py:8467` | D | 仅定义；换窗判定仍在使用其他 helper，此 marker 规则的弃用边界不明。 |
| `_build_recent_context_block` | `gateway.py:9024` | D | 仅定义；近期桶背景构建未接入，是否与现有 handoff 背景等价不明。 |
| `_should_inject_recent_context` | `gateway.py:9058` | D | 仅定义；近期背景 gate 未接入，配置字段仍存在，恢复条件不明。 |
| `_build_just_now_chat_context` | `gateway.py:9564` | D | 仅定义；just-now 配置/诊断仍在，是否保留该短时原文入口不明。 |
| `_format_direct_moment` | `gateway.py:12236` | D | 两处同名定义均非调用；旧 moment 直接展示与现行整桶展示的兼容边界不明。 |
| `_build_moment_diffused_memory_block` | `gateway.py:12672` | D | 仅定义；moment 扩散与桶扩散并存，旧入口是否供人工复原不明。 |
| `_moment_has_reliable_topic_evidence_for_diffusion_seed` | `gateway.py:13004` | D | 仅定义；旧 moment seed 主题审核的弃用范围未被历史明确说明。 |
| `_diffusion_path_has_source_record_topic_evidence` | `gateway.py:13303` | D | 仅定义；source-record 路径证据 helper 是否保留给 moment 线路不明。 |
| `_moment_candidate_confidence` | `gateway.py:13360` | D | 仅定义；moment 多分数置信度口径是否仍需旧兼容值不明。 |
| `_diffusion_path_has_date_neighbor` | `gateway.py:13385` | D | 仅定义；日期邻居扩散判定是否应随旧 moment 路线删除不明。 |
| `_query_requires_topic_evidence` | `gateway.py:13605` | D | 仅定义；旧 planner 便捷接口无明确弃用/兼容说明。 |
| `_allows_caution_diffusion` | `gateway.py:13718` | D | 仅定义；caution diffusion 的旧 planner 便捷接口无明确弃用说明。 |
| `_build_related_memory_block` | `gateway.py:14212` | D | 仅定义；related→diffused 兼容转发没有明确保留或废弃说明。 |
| `_shadow_specific_topic_terms` | `gateway.py:15040` | D | 仅定义；topic plan 的便捷接口是否仍承担调试兼容不明。 |
| `_emotional_reason_lookup_terms` | `gateway.py:16067` | D | 仅定义；情绪检索仍用 plan，旧取词口径与兼容需求不明。 |
| `_merge_dynamic_bucket_items` | `gateway.py:16965` | C | 6f1e317 撤下旧 supplemental/relation-axis 合并选卡，改用中性候选与统一 Utility。 |
| `_bucket_evidence_labels` | `gateway.py:17992` | D | 仅定义；旧 evidence 标签用于诊断还是应随 admission 全删，缺明确依据。 |
| `_hard_bucket_evidence_labels` | `gateway.py:18058` | D | 仅定义；旧 hard evidence 标签分类的诊断兼容需求不明。 |
| `_weak_bucket_evidence_block_reason` | `gateway.py:18079` | D | 仅定义；旧 weak evidence 拒绝原因的诊断兼容需求不明。 |
| `_boost_explicit_relation_edge_bucket_items` | `gateway.py:18295` | C | 6f1e317 撤下旧关系边候选加分调用，统一候选/Utility 接替。 |
| `_axis_lite_bucket_rejection` | `gateway.py:18579` | C | 6f1e317 撤下 admission 的 axis-lite 拒绝调用，统一 relevance 接替。 |
| `_bucket_is_tech_domain` | `gateway.py:18618` | C | 6f1e317 撤下旧 admission 的 tech 域判定调用，统一 relevance 接替。 |
| `_get_semantic_candidates` | `gateway.py:18802` | D | 仅定义；with_status 版本仍用，旧 scores-only 接口的兼容需求不明。 |
| `_hook_recall_cards_from_debug` | `gateway.py:20908` | D | 仅定义；fast cards 已在用，但 debug 转卡片是否保留给排障不明。 |
| `_hook_recall_how_to_apply` | `gateway.py:20977` | D | 仅定义；旧 hook 使用说明与现行卡片说明的保留契约不明。 |
| `_prepend_dynamic_context_to_user_message` | `gateway.py:21288` | D | 仅定义；当前注入/缓存策略已变，旧 user-message 拼接的回退用途不明。 |
| `aliases_for_canonical` | `identity_semantics.py:159` | D | 仅定义；身份别名查询是公共 store 方法，外部/维修使用契约不明。 |
| `role_edge_alias_config` | `identity_semantics.py:199` | D | 仅定义；身份索引到 role-edge 配置的导出用途尚未被明确废弃。 |
| `can_bucket_diffuse` | `memory_layers.py:330` | D | 仅定义；policy.can_diffuse 仍在，公共便捷 API 的兼容范围不明。 |
| `list_for_bucket` | `memory_moments.py:294` | D | 仅定义；moment 持久层仍在使用，按桶查询公共方法的兼容范围不明。 |
| `list_for_bucket_aliases` | `memory_moments.py:324` | D | 仅定义；retrieval alias 持久层仍使用，按桶查询方法的兼容范围不明。 |
| `emotional_recall_terms` | `memory_relevance.py:752` | D | 仅定义；emotional_recall_plan 仍用，公共取词包装的兼容范围不明。 |
| `list_recent` | `memory_write_gate.py:250` | D | 仅定义；write gate 日志仍写入，最近候选公共查询的维修用途不明。 |
| `POST_REPLY_EVALUATION_PROMPT` | `persona_engine.py:49` | C | _post_reply_evaluation_prompt 按实例身份渲染模板，旧静态值无开关。 |
| `FALLBACK_GUIDANCE` | `persona_engine.py:53` | C | 实例 fallback_guidance 按当前 AI 身份构造，旧常量无调用。 |
| `update_from_user_message` | `persona_engine.py:335` | D | 仅定义；pre-reply guidance 仍用，旧方法名转发是否兼容外部调用不明。 |
| `allows_moment_context` | `recall_policy.py:2992` | D | 仅定义；moment topic policy 仍在，公共 context gate 的弃用范围不明。 |
| `allows_bucket_context` | `recall_policy.py:3003` | D | 仅定义；bucket topic policy 仍在，公共 context gate 的弃用范围不明。 |
| `REFLECT_PROMPT` | `reflection_engine.py:349` | C | _reflect_prompt 按实例身份渲染模板，旧静态值无开关。 |
| `DIARY_MEMORY_PROMPT` | `reflection_engine.py:350` | C | _diary_memory_prompt 按实例身份渲染模板，旧静态值无开关。 |
| `_fallback_memory_anchor` | `reflection_engine.py:4237` | C | 28a8611 撤下固定 anchor 回退并将此函数改为空字典桩；无调用。 |
| `_scene_from_text` | `reflection_engine.py:4312` | C | 28a8611 删除固定 anchor 回退中的唯一调用；当前 anchor 由模型生成。 |
| `set_status` | `reminder_store.py:230` | D | 仅定义；reminder 状态存储仍在使用，旧状态写入 API 的兼容需求不明。 |
| `build_cross_bucket_edges` | `scripts/build_moment_graph.py:264` | D | 仅定义；with_stats 版本接替脚本入口，旧无统计公共 API 的兼容需求不明。 |
| `_format_handoff_darkroom_door` | `server.py:1919` | A | server.py:1897 handoff 有调用，tests/test_rooms.py:122 提取该函数验证。 |
| `_format_handoff_profile_facts` | `server.py:1928` | D | 仅定义；画像由 portrait 维护接替，但旧 profile-fact handoff 回退意图不明。 |
| `_format_handoff_relationship_weather` | `server.py:1960` | D | 仅定义；关系天气读取仍存在，旧 handoff 汇总的保留意图不明。 |
| `_auto_generate_moment_if_missing` | `server.py:2416` | D | 仅定义；自然正文写入已接替分段写入，旧 section fallback 的兼容范围不明。 |
| `_is_self_anchor_write_content` | `server.py:2438` | D | 仅定义；self-anchor 判断仍在，旧写入包装的兼容范围不明。 |
| `_save_password_hash` | `server.py:3256` | D | 仅定义；密码哈希读取/验证仍在，旧文件写入口是否供人工维修不明。 |
| `_verify_any_password` | `server.py:3275` | D | 仅定义；环境密码及存储哈希仍使用，旧统一校验入口的兼容需求不明。 |
| `enrich_backfill` | `server.py:3927` | B | 定义明确用于 enrich_on_write 超时/关闭后的人工维修；保留可复用入口。 |
| `edge_backfill` | `server.py:4183` | B | 定义及 config.example.yaml 明确定向 edge-only 维修用途，支持 bucket_id/query/dry_run。 |
| `_format_direct_moment` | `server.py:4696` | D | 两处同名定义均非调用；MCP moment 展示与当前整桶路径的兼容范围不明。 |
| `_query_requires_direct_topic_evidence` | `server.py:6220` | D | 仅定义；planner topic 判定仍用，旧便捷接口的兼容范围不明。 |
| `_secondary_direct_limit` | `server.py:6561` | D | 仅定义；secondary direct 规划仍在，旧便捷接口的兼容范围不明。 |
| `_build_mcp_moment_diffused_memory_block` | `server.py:6664` | D | 仅定义；MCP moment 扩散与其他图路径并存，旧入口弃用范围不明。 |
| `resurface` | `server.py:8739` | D | 仅定义；普通 breath 的配置浮现仍用同类能力，独立入口是否可弃用不明。 |
| `darkroom_status` | `server.py:9434` | B | 定义明确兼容旧 Dashboard；当前房间 REST 接口接替，保留恢复兼容入口。 |
| `dream` | `server.py:10516` | B | 定义明确兼容旧客户端 dream，当前由 introspection 接替。 |
| `portrait_maintain` | `server.py:10539` | D | 仅定义；REST 直接维护 portrait，旧直接函数是否供人工维修不明。 |
| `portrait_state` | `server.py:10595` | D | 字符串命中是状态文件名；REST 直接读 payload，旧函数兼容需求不明。 |

统计：A 1、B 4、C 15、D 50。

## 连带删除与文档核对

- `gateway.py:18368`（原位置）`_relation_query_bucket_title_match`：仅被删除的旧关系边加分函数调用；删除后再次 rg 确认只剩定义。
- `dehydrator.py:78`（原位置）`DEHYDRATE_PROMPT` 及 `Dehydrator.dehydrate_prompt` 的初始化赋值（原第 300 行）：仅服务 `_api_dehydrate`；逐项 rg 确认无其他代码引用后删除。
- dehydrator/persona/reflection 三个文件中的 `generic_identity_names` import：仅供被删除的静态 prompt 初始化；逐文件 rg 确认无剩余引用。
- 其他被删函数使用的辅助函数与 import 已核对：仍有调用或测试引用，不删。
- README.md 与 docs/reference.md 对所有被删除符号及连带项的精确词搜索无命中；未删除整模块或改变运行行为，因此两份正式文档无需同步。旧 BEHAVIOR_SPEC.md 中有 prompt 名称历史描述，属于 H-02 旧文档，本任务不扩展修改。

## 验证

- `.venv/bin/python -m pytest -q -x`：366 passed、0 failed，10 subtests passed，54.30 秒。
- `.venv/bin/python -c "import server"`：退出码 0。
- 1 条既有 Pydantic lifespan forward-reference warning，不影响通过。
- `git diff --check` 通过；AST 对比确认保留函数体及 MCP/REST 注册未改（仅移除孤儿 dehydrate_prompt 初始化赋值）。
- 本次不用部署；未 push、未合并、未修改 main。
- 验收重点：50 个 D 项需要明确兼容/人工维修/旧召回能力的保留口径后才能继续清理；B 是可复用代码入口，当前没有 MCP 注册，不表示旧客户端现在可以直接调用。

## 每项引用与历史证据

以下保留审查时的引用位置（修改前）及用户要求的历史末三条。历史末三条只解释引入背景，停用结论还以当前路径/相关 diff 为准。

### 119 · bucket_manager.py · record_surface_trace

历史：`60da4bd docs: make AGENTS.md the entry and move CLAUDE.md content to docs/reference.md`；`5c4cad3 fix bug`；`e6f60af 二改部分功能接入`。

引用（修改前，文档/注释命中不代表调用）：
```text
./bucket_manager.py:1924:    def record_surface_trace(self, items):
./docs/reference.md:370:`buckets/hit_stats.json` 持久化。`search()` 调用 `record_hit()` 记录（debounce 10 次或强制刷新）。`api_breath_debug` 有 query 时也记录。`record_surface_trace()` 记录 breath 无查询浮现。
```

### 124 · darkroom.py · continue_context

历史：`b0c8d37 Stop tracking test files`；`dcc565f Remove darkroom completeness gate`；`3426f33 Use room drafts for darkroom`。

引用（修改前，文档/注释命中不代表调用）：
```text
./darkroom.py:239:    def continue_context(self, limit: int = 3) -> dict:
```

### 126 · dehydrator.py · DIGEST_PROMPT

历史：`02ac292 Configure identity names for prompts`；`ccdffdb spec: add BEHAVIOR_SPEC and fix B-01~B-10 (resolved/decay/scoring)`；`0d695f7 init: first commit to Gitea mirror, update README with Docker quick start and new repo URL`。

引用（修改前，文档/注释命中不代表调用）：
```text
./BEHAVIOR_SPEC.md:150:3. **日记拆分**（正常路径）：`dehydrator.digest(content)` → `_api_digest()` → LLM 调用 `DIGEST_PROMPT`
./BEHAVIOR_SPEC.md:471:     │                    DIGEST_PROMPT → LLM API
./dehydrator.py:182:DIGEST_PROMPT = _render_dehydrator_template(DIGEST_PROMPT_TEMPLATE, generic_identity_names())
./server.py:9554:    # it sends the full DIGEST_PROMPT (~800 tokens) to DeepSeek for nothing.
```

### 127 · dehydrator.py · MERGE_PROMPT

历史：`1cf5ed9 Fix grow structured memory preservation`；`ccdffdb spec: add BEHAVIOR_SPEC and fix B-01~B-10 (resolved/decay/scoring)`；`0d695f7 init: first commit to Gitea mirror, update README with Docker quick start and new repo URL`。

引用（修改前，文档/注释命中不代表调用）：
```text
./BEHAVIOR_SPEC.md:492:             MERGE_PROMPT → LLM             importance, domain,
./dehydrator.py:207:MERGE_PROMPT = render_identity_template(MERGE_PROMPT_TEMPLATE, generic_identity_names())
```

### 129 · dehydrator.py · _api_dehydrate

历史：`d4740f0 fix: dehydrate优先走API + SQLite缓存, breath/pulse显示bucket_id`；`faf80fe fix: improve merge logic, time decay, breath output format, session hook`；`0d695f7 init: first commit to Gitea mirror, update README with Docker quick start and new repo URL`。

引用（修改前，文档/注释命中不代表调用）：
```text
./dehydrator.py:396:    async def _api_dehydrate(self, content: str) -> str:
```

### 130 · dream_engine.py · _normalize_model_result

历史：`32f894e Add latent night dream worker`。

引用（修改前，文档/注释命中不代表调用）：
```text
./dream_engine.py:722:    def _normalize_model_result(self, raw: dict) -> tuple[str, dict, list[str]]:
```

### 131 · gateway.py · SEMANTIC_RESCUE_SYSTEM_PROMPT

历史：`6f1e317 refactor(recall): unify candidate retrieval and contextual fallback`；`185ee77 Add evidence-gated recall activation`。

引用（修改前，文档/注释命中不代表调用）：
```text
./gateway.py:157:SEMANTIC_RESCUE_SYSTEM_PROMPT = """You are a strict memory evidence verifier.
```

### 134 · gateway.py · _update_persona_after_response

历史：`9fc2232 Add just-now chat context`；`55eb3a3 Refactor persona state post-reply updates`。

引用（修改前，文档/注释命中不代表调用）：
```text
./gateway.py:6365:    async def _update_persona_after_response(
```

### 135 · gateway.py · _query_has_handoff_transition_marker

历史：`060cc33 调整gateway注入逻辑`；`00296b7 Fix handoff just-now recall and retry dedupe`。

引用（修改前，文档/注释命中不代表调用）：
```text
./gateway.py:8475:    def _query_has_handoff_transition_marker(query_text: str) -> bool:
```

### 136 · gateway.py · _build_recent_context_block

历史：`060cc33 调整gateway注入逻辑`；`89ed93c Add gateway persona state engine`。

引用（修改前，文档/注释命中不代表调用）：
```text
./gateway.py:9032:    async def _build_recent_context_block(
```

### 137 · gateway.py · _should_inject_recent_context

历史：`060cc33 调整gateway注入逻辑`；`9d52eea Gate gateway recent context injection`。

引用（修改前，文档/注释命中不代表调用）：
```text
./gateway.py:9066:    def _should_inject_recent_context(
```

### 138 · gateway.py · _build_just_now_chat_context

历史：`060cc33 调整gateway注入逻辑`；`00296b7 Fix handoff just-now recall and retry dedupe`；`9fc2232 Add just-now chat context`。

引用（修改前，文档/注释命中不代表调用）：
```text
./gateway.py:9572:    def _build_just_now_chat_context(self, query_text: str) -> tuple[str, dict[str, Any]]:
```

### 139 · gateway.py · _format_direct_moment

历史：`e26afa8 Add auto direct bucket rendering`；`bb77a46 feat: inject gateway memory from moment graph`；`af6eba4 feat: recall memory moments as graph`。

引用（修改前，文档/注释命中不代表调用）：
```text
./gateway.py:12244:    def _format_direct_moment(
./server.py:4694:def _format_direct_moment(seed: dict, grouped: dict[str, list[dict]], token_budget: int) -> str:
```

### 142 · gateway.py · _build_moment_diffused_memory_block

历史：`af61ab8 Show gateway diffused chain debug`；`3563a7b feat: stabilize p0 memory recall gating`；`bb77a46 feat: inject gateway memory from moment graph`。

引用（修改前，文档/注释命中不代表调用）：
```text
./gateway.py:12680:    def _build_moment_diffused_memory_block(
```

### 143 · gateway.py · _moment_has_reliable_topic_evidence_for_diffusion_seed

历史：`1a4baad Improve recall routing P0`；`eda6be3 Add recall reading notes and metadata view`。

引用（修改前，文档/注释命中不代表调用）：
```text
./gateway.py:13012:    def _moment_has_reliable_topic_evidence_for_diffusion_seed(self, query: str, moment: dict) -> bool:
```

### 144 · gateway.py · _diffusion_path_has_source_record_topic_evidence

历史：`1a4baad Improve recall routing P0`。

引用（修改前，文档/注释命中不代表调用）：
```text
./gateway.py:13311:    def _diffusion_path_has_source_record_topic_evidence(path: Any) -> bool:
```

### 145 · gateway.py · _moment_candidate_confidence

历史：`a46c188 Tighten secondary direct diffusion evidence`；`078d317 fix: rank diffusion candidates before injection`。

引用（修改前，文档/注释命中不代表调用）：
```text
./gateway.py:13368:    def _moment_candidate_confidence(self, moment: dict, *, default: float = 0.72) -> float:
```

### 146 · gateway.py · _diffusion_path_has_date_neighbor

历史：`078d317 fix: rank diffusion candidates before injection`。

引用（修改前，文档/注释命中不代表调用）：
```text
./gateway.py:13393:    def _diffusion_path_has_date_neighbor(
```

### 147 · gateway.py · _query_requires_topic_evidence

历史：`b857f2e Tighten memory recall admission`；`d148e85 fix: gate recent context by explicit query topic`；`8403339 fix: align gateway explicit topic diffusion`。

引用（修改前，文档/注释命中不代表调用）：
```text
./gateway.py:13613:    def _query_requires_topic_evidence(self, query: str) -> bool:
```

### 148 · gateway.py · _allows_caution_diffusion

历史：`b06fdbc Centralize recall query planning`；`3563a7b feat: stabilize p0 memory recall gating`。

引用（修改前，文档/注释命中不代表调用）：
```text
./gateway.py:13726:    def _allows_caution_diffusion(self, query: str, context_mode: str) -> bool:
```

### 149 · gateway.py · _build_related_memory_block

历史：`b0c8d37 Stop tracking test files`；`4bfd27a feat: add memory diffusion recall`；`43f78bc Add reflection memory edges`。

引用（修改前，文档/注释命中不代表调用）：
```text
./gateway.py:14220:    async def _build_related_memory_block(
```

### 150 · gateway.py · _shadow_specific_topic_terms

历史：`1593981 fix(recall): ignore identity names in phase 1 shadow relevance`；`be76ef5 fix(recall): tighten phase 1 shadow relevance`。

引用（修改前，文档/注释命中不代表调用）：
```text
./gateway.py:15048:    def _shadow_specific_topic_terms(self, query: str) -> list[str]:
```

### 151 · gateway.py · _emotional_reason_lookup_terms

历史：`91607b9 fix: derive emotional recall anchors`；`ff307b2 fix: fallback emotional reason planner queries`。

引用（修改前，文档/注释命中不代表调用）：
```text
./gateway.py:16075:    def _emotional_reason_lookup_terms(self, query: str) -> list[str]:
```

### 152 · gateway.py · _merge_dynamic_bucket_items

历史：`6f1e317 refactor(recall): unify candidate retrieval and contextual fallback`；`dff225c Add relation axis supplemental recall`；`4488ce1 feat: add memory query planner and detail recall`。

引用（修改前，文档/注释命中不代表调用）：
```text
./gateway.py:16965:    def _merge_dynamic_bucket_items(self, items: list[dict], query: str) -> list[dict]:
```

### 153 · gateway.py · _bucket_evidence_labels

历史：`b0c8d37 Stop tracking test files`；`12e96a8 Demote generic game recall evidence`；`bb128b9 Tighten recall evidence gates`。

引用（修改前，文档/注释命中不代表调用）：
```text
./gateway.py:18057:    def _bucket_evidence_labels(self, query: str, item: dict) -> list[str]:
```

### 154 · gateway.py · _hard_bucket_evidence_labels

历史：`b0c8d37 Stop tracking test files`；`12e96a8 Demote generic game recall evidence`；`bb128b9 Tighten recall evidence gates`。

引用（修改前，文档/注释命中不代表调用）：
```text
./gateway.py:18123:    def _hard_bucket_evidence_labels(labels: list[str]) -> list[str]:
```

### 155 · gateway.py · _weak_bucket_evidence_block_reason

历史：`b0c8d37 Stop tracking test files`；`12e96a8 Demote generic game recall evidence`；`bb128b9 Tighten recall evidence gates`。

引用（修改前，文档/注释命中不代表调用）：
```text
./gateway.py:18144:    def _weak_bucket_evidence_block_reason(labels: list[str]) -> str:
```

### 156 · gateway.py · _boost_explicit_relation_edge_bucket_items

历史：`6f1e317 refactor(recall): unify candidate retrieval and contextual fallback`；`dff225c Add relation axis supplemental recall`；`1a4baad Improve recall routing P0`。

引用（修改前，文档/注释命中不代表调用）：
```text
./gateway.py:18295:    def _boost_explicit_relation_edge_bucket_items(self, query: str, items: list[dict]) -> list[dict]:
```

### 157 · gateway.py · _axis_lite_bucket_rejection

历史：`6f1e317 refactor(recall): unify candidate retrieval and contextual fallback`；`1a4baad Improve recall routing P0`。

引用（修改前，文档/注释命中不代表调用）：
```text
./gateway.py:18579:    def _axis_lite_bucket_rejection(
```

### 158 · gateway.py · _bucket_is_tech_domain

历史：`6f1e317 refactor(recall): unify candidate retrieval and contextual fallback`；`2dec58a Harden recall admission for tech memories`。

引用（修改前，文档/注释命中不代表调用）：
```text
./gateway.py:18618:    def _bucket_is_tech_domain(self, bucket: dict | None) -> bool:
```

### 159 · gateway.py · _get_semantic_candidates

历史：`eda6be3 Add recall reading notes and metadata view`；`b2fc807 Bound dynamic recall embedding latency`；`89ed93c Add gateway persona state engine`。

引用（修改前，文档/注释命中不代表调用）：
```text
./gateway.py:18970:    async def _get_semantic_candidates(self, query: str, eligible_ids: set[str]) -> dict[str, float]:
```

### 160 · gateway.py · _hook_recall_cards_from_debug

历史：`8957c39 Soften hook recall memory notes`；`b84379f Skip empty hook recall cards`；`6e3a36b Add hook recall endpoint`。

引用（修改前，文档/注释命中不代表调用）：
```text
./gateway.py:21076:    def _hook_recall_cards_from_debug(
```

### 161 · gateway.py · _hook_recall_how_to_apply

历史：`3882f97 feat: activate rebuilt recall decisions`；`6e3a36b Add hook recall endpoint`。

引用（修改前，文档/注释命中不代表调用）：
```text
./gateway.py:21145:    def _hook_recall_how_to_apply() -> str:
```

### 162 · gateway.py · _prepend_dynamic_context_to_user_message

历史：`eaed9bf 修改prompt拼接和缓存策略`；`2205e4d Improve gateway cache-friendly memory injection`。

引用（修改前，文档/注释命中不代表调用）：
```text
./gateway.py:21456:    def _prepend_dynamic_context_to_user_message(
```

### 164 · identity_semantics.py · aliases_for_canonical

历史：`b0c8d37 Stop tracking test files`；`a0b25ca feat: add word map identity indexes`。

引用（修改前，文档/注释命中不代表调用）：
```text
./identity_semantics.py:159:    def aliases_for_canonical(self, canonical: str) -> list[dict[str, Any]]:
```

### 167 · identity_semantics.py · role_edge_alias_config

历史：`a0b25ca feat: add word map identity indexes`。

引用（修改前，文档/注释命中不代表调用）：
```text
./identity_semantics.py:199:    def role_edge_alias_config(self) -> dict[str, Any]:
```

### 170 · memory_layers.py · can_bucket_diffuse

历史：`b0c8d37 Stop tracking test files`；`2cd559d Add memory layer policy rules`。

引用（修改前，文档/注释命中不代表调用）：
```text
./memory_layers.py:330:def can_bucket_diffuse(bucket: dict[str, Any] | None) -> bool:
```

### 172 · memory_moments.py · list_for_bucket

历史：`2744c9e Add dashboard bulk bucket deletion`；`af6eba4 feat: recall memory moments as graph`；`a230b5f feat: add memory moment inspection`。

引用（修改前，文档/注释命中不代表调用）：
```text
./memory_moments.py:294:    def list_for_bucket(self, bucket_id: str, limit: int = 100) -> list[dict]:
```

### 173 · memory_moments.py · list_for_bucket_aliases

历史：`185ee77 Add evidence-gated recall activation`。

引用（修改前，文档/注释命中不代表调用）：
```text
./memory_moments.py:324:    def list_for_bucket_aliases(self, bucket_id: str, limit: int = 100) -> list[dict]:
```

### 175 · memory_relevance.py · emotional_recall_terms

历史：`b0c8d37 Stop tracking test files`；`91607b9 fix: derive emotional recall anchors`。

引用（修改前，文档/注释命中不代表调用）：
```text
./memory_relevance.py:752:def emotional_recall_terms(
```

### 176 · memory_write_gate.py · list_recent

历史：`2b6da13 feat: lighten persona evaluation`；`9fc2232 Add just-now chat context`；`9558ed6 feat: gate automatic grow memory writes`。

引用（修改前，文档/注释命中不代表调用）：
```text
./memory_write_gate.py:250:    def list_recent(self, limit: int = 20) -> list[dict]:
```

### 177 · persona_engine.py · POST_REPLY_EVALUATION_PROMPT

历史：`02ac292 Configure identity names for prompts`；`7598653 Localize persona event summaries`；`55eb3a3 Refactor persona state post-reply updates`。

引用（修改前，文档/注释命中不代表调用）：
```text
./persona_engine.py:49:POST_REPLY_EVALUATION_PROMPT = render_identity_template(
```

### 178 · persona_engine.py · FALLBACK_GUIDANCE

历史：`55eb3a3 Refactor persona state post-reply updates`；`637001d 增加心跳tab`；`89ed93c Add gateway persona state engine`。

引用（修改前，文档/注释命中不代表调用）：
```text
./persona_engine.py:53:FALLBACK_GUIDANCE = "根据当前状态自然回应，不解释隐藏状态。"
```

### 181 · persona_engine.py · update_from_user_message

历史：`95e3198 Add DeepSeek thinking mode config for dehydration and persona`；`637001d 增加心跳tab`；`89ed93c Add gateway persona state engine`。

引用（修改前，文档/注释命中不代表调用）：
```text
./persona_engine.py:340:    async def update_from_user_message(self, session_id: str, user_message: str) -> dict:
```

### 188 · recall_policy.py · allows_moment_context

历史：`5e9fc33 Unify recall topic evidence policy`。

引用（修改前，文档/注释命中不代表调用）：
```text
./recall_policy.py:2992:    def allows_moment_context(
```

### 189 · recall_policy.py · allows_bucket_context

历史：`5e9fc33 Unify recall topic evidence policy`。

引用（修改前，文档/注释命中不代表调用）：
```text
./recall_policy.py:3003:    def allows_bucket_context(
```

### 190 · reflection_engine.py · REFLECT_PROMPT

历史：`02ac292 Configure identity names for prompts`；`64a503d Diversify reflection affect anchors`；`43f78bc Add reflection memory edges`。

引用（修改前，文档/注释命中不代表调用）：
```text
./reflection_engine.py:349:REFLECT_PROMPT = render_identity_template(REFLECT_PROMPT_TEMPLATE, generic_identity_names())
```

### 191 · reflection_engine.py · DIARY_MEMORY_PROMPT

历史：`02ac292 Configure identity names for prompts`；`6956c63 Add diary-aware reflection extraction`。

引用（修改前，文档/注释命中不代表调用）：
```text
./reflection_engine.py:350:DIARY_MEMORY_PROMPT = render_identity_template(
```

### 192 · reflection_engine.py · _fallback_memory_anchor

历史：`28a8611 Tighten affect anchor generation`；`05c74d3 Add ChatGPT OAuth and reflection anchors`。

引用（修改前，文档/注释命中不代表调用）：
```text
./reflection_engine.py:4237:    def _fallback_memory_anchor(self, bucket: dict, tags: list[str]) -> dict:
```

### 193 · reflection_engine.py · _scene_from_text

历史：`28a8611 Tighten affect anchor generation`；`05c74d3 Add ChatGPT OAuth and reflection anchors`。

引用（修改前，文档/注释命中不代表调用）：
```text
./reflection_engine.py:4312:    def _scene_from_text(self, title: str, content: str) -> str:
```

### 195 · reminder_store.py · set_status

历史：`641ed7c Handle legacy todo writeback dates`；`562b5bf Fix todo writeback completion dates`；`5bdd418 Tighten recall routing and add todo followup store`。

引用（修改前，文档/注释命中不代表调用）：
```text
./reminder_store.py:230:    def set_status(self, reminder_id: str, status: str, *, resolved_at: str | None = None) -> dict | None:
```

### 196 · scripts/build_moment_graph.py · build_cross_bucket_edges

历史：`f151690 Add moment graph dry-run diagnostics`；`69de57a Tighten local graph edge evidence`；`4c26e28 Add local moment graph worker`。

引用（修改前，文档/注释命中不代表调用）：
```text
./scripts/build_moment_graph.py:264:def build_cross_bucket_edges(
```

### 204 · server.py · _format_handoff_darkroom_door

历史：`c5d0b8b feat: complete room B snapshot and handoff support`；`de56433 fix: hide darkroom and persona from handoff panels`；`029f76d Add private darkroom tools`。

引用（修改前，文档/注释命中不代表调用）：
```text
./server.py:1897:        ("言之的房间", _format_handoff_darkroom_door(), 45, False),
./server.py:1919:def _format_handoff_darkroom_door() -> str:
./tests/test_rooms.py:122:    names = {"_build_handoff_breath", "_format_handoff_darkroom_door", "_handoff_portrait_stable_body"}
```

### 205 · server.py · _format_handoff_profile_facts

历史：`a2d2ee1 fix: keep handoff portraits to core mid terms`；`5201408 Add portrait handoff maintainer`。

引用（修改前，文档/注释命中不代表调用）：
```text
./server.py:1928:def _format_handoff_profile_facts(all_buckets: list[dict], limit: int = 6) -> str:
```

### 206 · server.py · _format_handoff_relationship_weather

历史：`a2d2ee1 fix: keep handoff portraits to core mid terms`；`5201408 Add portrait handoff maintainer`。

引用（修改前，文档/注释命中不代表调用）：
```text
./server.py:1960:def _format_handoff_relationship_weather(all_buckets: list[dict]) -> str:
```

### 207 · server.py · _auto_generate_moment_if_missing

历史：`3a09dcd fix: add configurable self anchor entry`；`47e1bdb fix: gate self anchor tag reads`；`5374f8c fix: keep auto moment as summary section`。

引用（修改前，文档/注释命中不代表调用）：
```text
./server.py:2416:async def _auto_generate_moment_if_missing(content: str, *, section_fallback: bool = False) -> str:
```

### 208 · server.py · _is_self_anchor_write_content

历史：`b491982 feat(memory): write buckets as natural prose instead of section headings`；`47e1bdb fix: gate self anchor tag reads`。

引用（修改前，文档/注释命中不代表调用）：
```text
./server.py:2438:def _is_self_anchor_write_content(
```

### 209 · server.py · _save_password_hash

历史：`ccdffdb spec: add BEHAVIOR_SPEC and fix B-01~B-10 (resolved/decay/scoring)`。

引用（修改前，文档/注释命中不代表调用）：
```text
./server.py:3256:def _save_password_hash(password: str) -> None:
```

### 210 · server.py · _verify_any_password

历史：`ccdffdb spec: add BEHAVIOR_SPEC and fix B-01~B-10 (resolved/decay/scoring)`。

引用（修改前，文档/注释命中不代表调用）：
```text
./server.py:3275:def _verify_any_password(password: str) -> bool:
```

### 219 · server.py · enrich_backfill

历史：`7b71a0d Add one-click deployment menu`；`52e7071 Document enrich backfill tool`；`45012ab Fix nonblocking memory write backfills`。

引用（修改前，文档/注释命中不代表调用）：
```text
./server.py:3926:async def enrich_backfill(limit: int = 10) -> dict:
```

### 220 · server.py · edge_backfill

历史：`6a62af6 docs: archive handoff notes and clarify favorite memory`；`e649d7e Update README for mainline handoff release`；`739cc93 Add edge-only memory backfill`。

引用（修改前，文档/注释命中不代表调用）：
```text
./server.py:4181:async def edge_backfill(
./config.example.yaml:645:  edge_backfill_limit: 5             # default limit for manual edge_backfill; only writes memory_edges
```

### 223 · server.py · _format_direct_moment

历史：`e26afa8 Add auto direct bucket rendering`；`bb77a46 feat: inject gateway memory from moment graph`；`af6eba4 feat: recall memory moments as graph`。

引用（修改前，文档/注释命中不代表调用）：
```text
./gateway.py:12244:    def _format_direct_moment(
./server.py:4694:def _format_direct_moment(seed: dict, grouped: dict[str, list[dict]], token_budget: int) -> str:
```

### 224 · server.py · _query_requires_direct_topic_evidence

历史：`b06fdbc Centralize recall query planning`；`250ed79 Expose runtime gates in memory debug`；`b857f2e Tighten memory recall admission`。

引用（修改前，文档/注释命中不代表调用）：
```text
./server.py:6218:def _query_requires_direct_topic_evidence(query: str) -> bool:
```

### 225 · server.py · _secondary_direct_limit

历史：`b06fdbc Centralize recall query planning`；`1209788 feat: surface body concept chains in breath`。

引用（修改前，文档/注释命中不代表调用）：
```text
./server.py:6559:def _secondary_direct_limit(query: str, related_per_memory: int) -> int:
```

### 226 · server.py · _build_mcp_moment_diffused_memory_block

历史：`3563a7b feat: stabilize p0 memory recall gating`；`af6eba4 feat: recall memory moments as graph`。

引用（修改前，文档/注释命中不代表调用）：
```text
./server.py:6662:async def _build_mcp_moment_diffused_memory_block(
```

### 234 · server.py · resurface

历史：`59681d3 Refine fork documentation`；`7cfdf70 Rewrite fork deployment README`；`53dc731 Add ring comments and whisper memories`。

引用（修改前，文档/注释命中不代表调用）：
```text
./dashboard.html:3116:        <select id="cfg-query-resurface-enabled">
./dashboard.html:8265:    document.getElementById('cfg-query-resurface-enabled').value =
./dashboard.html:8515:      query_resurface_enabled: document.getElementById('cfg-query-resurface-enabled').value === 'true',
./INTERNALS.md:86:| `resurface` | max_results, include_archive, max_tokens | 只读浮现久未触碰的旧记忆 |
./CLAUDE_PROMPT.md:75:普通查询默认不会随机漂旧桶。若部署显式开启 `recall.query_resurface_enabled`，低命中且没有相关联想时可能追加 `[surface_type: resurface]` 的久未触碰旧记忆；把它当可忽略的回响，不当直接命中。
./recall_policy.py:239:        "resurface",
./recall_policy.py:291:        "resurface",
./server.py:17:#       resurface — Surface dormant memories without touching them
./server.py:8624:                    entry = f"[surface_type: resurface, dormant_days={dormant_days:.0f}]\n{summary}"
./server.py:8734:# Tool 1.4: resurface — dormant memory resurfacing
./server.py:8735:# 工具 1.4：resurface — 久未触碰记忆浮现
./server.py:8737:async def resurface(max_results: int = 1, include_archive: bool = True, max_tokens: int = 800) -> str:
```

### 244 · server.py · darkroom_status

历史：`b0c8d37 Stop tracking test files`；`1fb71a3 Add darkroom visibility states`；`029f76d Add private darkroom tools`。

引用（修改前，文档/注释命中不代表调用）：
```text
./server.py:9431:async def darkroom_status() -> dict:
```

### 250 · server.py · dream

历史：`d2d4b89 fix(search): keep resolved buckets reachable by keyword`；`ccdffdb spec: add BEHAVIOR_SPEC and fix B-01~B-10 (resolved/decay/scoring)`；`821546d docs: update README/INTERNALS for import feature, harden .gitignore`。

引用（修改前，文档/注释命中不代表调用）：
```text
./BEHAVIOR_SPEC.md:26:| `server.py` | 注册 MCP 工具（`breath/hold/grow/trace/pulse/dream`）；路由 Dashboard HTTP 请求；`_merge_or_create()` 合并逻辑中枢 |
./BEHAVIOR_SPEC.md:78:2. dream()                — 消化最近记忆，有沉淀写 feel
./BEHAVIOR_SPEC.md:319:### 场景 9：用户使用 dream 工具进行记忆沉淀
./BEHAVIOR_SPEC.md:321:**触发**：Claude 在对话启动时，`breath()` 之后调用 `dream()`
./BEHAVIOR_SPEC.md:323:**OB 工具调用**：`dream()`（无参数）
./BEHAVIOR_SPEC.md:348:**触发**：Claude 在 dream 后决定记录某段记忆带来的感受
./BEHAVIOR_SPEC.md:360:4. `embedding_engine.generate_and_store(bucket_id, content)` — feel 桶同样有向量（供 dream 结晶检测使用）
./BEHAVIOR_SPEC.md:450:| `dream()` | embedding 未开启 | 跳过连接提示和结晶提示，仅返回记忆列表 |
./BEHAVIOR_SPEC.md:451:| `dream()` | 桶列表为空 | 返回 `"没有需要消化的新记忆。"` |
./BEHAVIOR_SPEC.md:523:          │  被 dream() 消化:                                        │
./BEHAVIOR_SPEC.md:559:         ├─→ embedding_engine.generate_and_store()（供 dream 结晶检测）
./BEHAVIOR_SPEC.md:616:- `dream()` 连接提示（best_sim > 0.5）+ 结晶提示（feel 相似度 > 0.7 × ≥2 个）
./utils.py:502:        "dream": {
./utils.py:782:        config.setdefault("dream", {})["api_key"] = env_dream_api_key
./utils.py:786:        config.setdefault("dream", {})["base_url"] = env_dream_base_url
./utils.py:790:        config.setdefault("dream", {})["model"] = env_dream_model
./utils.py:794:        config.setdefault("dream", {})["enabled"] = env_dream_enabled.lower() in (
./dream_engine.py:23:logger = logging.getLogger("ombre_brain.dream")
./dream_engine.py:104:    """Night-Fall style latent dream generation and breath-gated surfacing."""
./dream_engine.py:109:        cfg = config.get("dream", {}) if isinstance(config.get("dream", {}), dict) else {}
./dream_engine.py:206:            raise ValueError("dream record missing frontmatter")
./dream_engine.py:211:            raise ValueError("dream metadata must be a mapping")
./dream_engine.py:220:                logger.warning("Failed to read dream record %s: %s", path, exc)
./dream_engine.py:239:                logger.warning("Failed to delete dream embedding %s: %s", record.dream_id, exc)
./dream_engine.py:609:            raise RuntimeError("dream model api key is not configured")
./dream_engine.py:628:            raise ValueError("dream model returned empty text")
./dashboard.html:2022:  .dream-shell { max-width: 760px; margin: 0 auto; }
./dashboard.html:2023:  .dream-toolbar { margin-bottom: 18px; }
./dashboard.html:2024:  .dream-toolbar h2 {
./dashboard.html:2030:  .dream-toolbar p {
./dashboard.html:2035:  .dream-list {
./dashboard.html:2040:  .dream-row {
./dashboard.html:2047:  .dream-row-toggle,
./dashboard.html:2048:  .dream-row-static {
./dashboard.html:2056:  .dream-row-toggle {
./dashboard.html:2064:  .dream-row-toggle:hover { background: rgba(47, 79, 79, 0.045); }
./dashboard.html:2065:  .dream-row-toggle:focus-visible {
./dashboard.html:2069:  .dream-row-title { color: var(--text); font-size: 14px; }
./dashboard.html:2070:  .dream-row-status { color: var(--text-light); font-size: 12px; white-space: nowrap; }
./dashboard.html:2071:  .dream-body {
./dashboard.html:3280:  <div class="dream-shell">
./dashboard.html:3281:    <div class="dream-toolbar">
./dashboard.html:3593:        <select id="cfg-dream-engine-enabled"><option value="true">开启</option><option value="false">关闭</option></select>
./dashboard.html:3598:        <select id="cfg-dream-enabled"><option value="true">开启</option><option value="false">关闭</option></select>
./dashboard.html:3602:        <select id="cfg-dream-surface"><option value="true">开启</option><option value="false">关闭</option></select>
./dashboard.html:3606:        <select id="cfg-dream-inject"><option value="false">关闭</option><option value="true">开启</option></select>
./dashboard.html:3611:        <select id="cfg-dream-retain"><option value="false">关闭</option><option value="true">开启</option></select>
./dashboard.html:3616:        <input type="text" id="cfg-dream-model" placeholder="deepseek-v4-flash" />
./dashboard.html:3620:        <input type="text" id="cfg-dream-url" placeholder="https://api.deepseek.com" />
./dashboard.html:3624:        <input type="password" id="cfg-dream-key" placeholder="当前: 加载中…" />
./dashboard.html:3629:        <input type="number" id="cfg-dream-hour" min="0" max="23" />
./dashboard.html:3634:        <input type="number" id="cfg-dream-prob" min="0" max="1" step="0.05" />
./dashboard.html:3639:        <input type="number" id="cfg-dream-min" min="1" max="20" />
./dashboard.html:3644:        <input type="number" id="cfg-dream-window" min="1" max="168" />
./dashboard.html:3649:        <input type="text" id="cfg-dream-anchor" placeholder="c0b8ddb7423e" />
./dashboard.html:3849:  ['auth-login-pwd', 'auth-setup-pwd', 'auth-setup-pwd2', 'cfg-dehy-key', 'cfg-emb-key', 'cfg-persona-key', 'cfg-domain-sentinel-key', 'cfg-reflection-key', 'cfg-dream-key'].forEach(function(id) {
./dashboard.html:4734:    content.innerHTML = '<div class="dream-list">' + records.map(function(record) {
./dashboard.html:4738:        return '<div class="dream-row"><div class="dream-row-static">' +
./dashboard.html:4739:          '<div class="dream-row-title">' + title + '</div>' +
./dashboard.html:4740:          '<div class="dream-row-status">' + dreamStatusLabel(record.status) + '</div>' +
./dashboard.html:4743:      return '<div class="dream-row" data-dream-id="' + escAttr(record.dream_id) + '">' +
./dashboard.html:4744:        '<button type="button" class="dream-row-toggle" aria-expanded="false" onclick="toggleDreamPeek(this)">' +
./dashboard.html:4745:          '<span class="dream-row-title">' + title + '</span>' +
./dashboard.html:4746:          '<span class="dream-row-status">' + dreamStatusLabel(record.status) + '</span>' +
./dashboard.html:4748:        '<div class="dream-body" hidden></div>' +
./dashboard.html:4757:  var row = button.closest('.dream-row');
./dashboard.html:4758:  var body = row ? row.querySelector('.dream-body') : null;
./dashboard.html:4773:    var dreamId = row.getAttribute('data-dream-id') || '';
./dashboard.html:7690:        ? 'dream injected' + (dreamStatus.retained ? ' · retained' : '')
./dashboard.html:7691:        : 'dream skipped' + (dreamStatus.reason ? ' · ' + dreamStatus.reason : ''),
./dashboard.html:8211:    document.getElementById('cfg-dream-engine-enabled').value = cfg.dream.enabled ? 'true' : 'false';
./dashboard.html:8212:    document.getElementById('cfg-dream-enabled').value = cfg.dream.auto_enabled ? 'true' : 'false';
./dashboard.html:8213:    document.getElementById('cfg-dream-surface').value = cfg.dream.surface_enabled ? 'true' : 'false';
./dashboard.html:8214:    document.getElementById('cfg-dream-inject').value = cfg.dream.inject_enabled ? 'true' : 'false';
./dashboard.html:8215:    document.getElementById('cfg-dream-retain').value = cfg.dream.retain_after_inject ? 'true' : 'false';
./dashboard.html:8216:    document.getElementById('cfg-dream-model').value = cfg.dream.model || '';
./dashboard.html:8217:    document.getElementById('cfg-dream-url').value = cfg.dream.base_url || '';
./dashboard.html:8218:    document.getElementById('cfg-dream-key').placeholder = '当前: ' + (cfg.dream.api_key_masked || (cfg.dream.api_ready ? '***' : '未设置'));
./dashboard.html:8219:    document.getElementById('cfg-dream-key').value = '';
./dashboard.html:8220:    document.getElementById('cfg-dream-hour').value = cfg.dream.daily_hour ?? 3;
./dashboard.html:8221:    document.getElementById('cfg-dream-prob').value = cfg.dream.daily_probability ?? 0.4;
./dashboard.html:8222:    document.getElementById('cfg-dream-min').value = cfg.dream.min_material_count || 5;
./dashboard.html:8223:    document.getElementById('cfg-dream-window').value = cfg.dream.material_window_hours || 48;
./dashboard.html:8224:    document.getElementById('cfg-dream-anchor').value = cfg.dream.identity_anchor_id || '';
./dashboard.html:8328:      'Dream API: <strong>' + (cfg.dream.api_ready ? '已设置' : '未设置') + '</strong><br>' +
./dashboard.html:8412:    candidate.dream = {
./dashboard.html:8413:      enabled: document.getElementById('cfg-dream-engine-enabled').value === 'true',
./dashboard.html:8414:      auto_enabled: document.getElementById('cfg-dream-enabled').value === 'true',
./dashboard.html:8415:      surface_enabled: document.getElementById('cfg-dream-surface').value === 'true',
./dashboard.html:8416:      inject_enabled: document.getElementById('cfg-dream-inject').value === 'true',
./dashboard.html:8417:      retain_after_inject: document.getElementById('cfg-dream-retain').value === 'true',
./dashboard.html:8418:      model: document.getElementById('cfg-dream-model').value,
./dashboard.html:8419:      base_url: document.getElementById('cfg-dream-url').value,
./dashboard.html:8420:      daily_hour: numberValue('cfg-dream-hour', 3),
./dashboard.html:8421:      daily_probability: floatValue('cfg-dream-prob', 0.4),
./dashboard.html:8422:      min_material_count: numberValue('cfg-dream-min', 5),
./dashboard.html:8423:      material_window_hours: numberValue('cfg-dream-window', 48),
./dashboard.html:8424:      identity_anchor_id: document.getElementById('cfg-dream-anchor').value,
./dashboard.html:8550:    'dream',
./dashboard.html:8598:  var dreamKeyVal = document.getElementById('cfg-dream-key').value;
./dashboard.html:8600:    if (!body.dream) body.dream = {};
./dashboard.html:8601:    body.dream.api_key = dreamKeyVal;
./INTERNALS.md:62:- **Dream 做梦**（`dream()`）：返回最近 10 条 + 自省引导 + 连接提示 + 结晶化提示
./INTERNALS.md:63:- **对话启动流程**：breath() → dream() → breath(domain="feel") → 开始对话
./INTERNALS.md:85:| `dream` | （无） | 做梦自省 |
./INTERNALS.md:107:**`dream`** — 做梦/自省触发器：
./INTERNALS.md:125:- `digested=0/1`：隐藏/取消隐藏记忆（控制是否在 dream 中出现）
./INTERNALS.md:141:| `/dream-hook` | GET | Dream 钩子 |
./INTERNALS.md:169:- `/health`, `/breath-hook`, `/dream-hook`, `/mcp*` 路径不受保护（公开）
./INTERNALS.md:287:| `0.7` | `server.py` dream | feel 结晶相似度阈值 |
./INTERNALS.md:336:| `10` | `server.py` dream | 取最近 N 个桶 |
./INTERNALS.md:452:### 5.8 为什么 dream 设计成对话开头自动执行？
./INTERNALS.md:454:**决策**：每次新对话启动时，Claude 执行 `dream()` 消化最近记忆，有沉淀写 feel，能放下的 resolve。
./README.md:156:- `dream.surface_enabled`：允许 `breath()` 在共振或新会话条件下浮现梦。
./README.md:157:- `dream.inject_enabled`：允许 Gateway 静默加入一条 Dream Context；默认关闭。
./README.md:405:| `word_map` / `dream` | 默认可关闭的派生能力 |
./server.py:224:# OMBRE_HOOK_URL: 在 breath/dream 被调用后推送事件到该 URL（POST JSON）。
./server.py:299:dream_engine = DreamEngine(config)                     # Night dream worker / 夜梦
./server.py:3480:# 清醒自省专用挂载点。/dream-hook 暂时保留兼容旧接入。
./server.py:3483:@mcp.custom_route("/dream-hook", methods=["GET"])
./server.py:8064:                # dream() 去重用：标记"刚被 breath 浮现过"，不影响衰减打分
./server.py:8347:            response_sections.append("dream")
./server.py:8659:        response_sections.append("dream")
./server.py:10072:    # --- dream 去重：breath() 浮现窗口刚出现过的桶跳过，不重复返回全文 ---
./server.py:10073:    # --- (10分钟窗口，覆盖"开窗三件套" breath→dream→breath(feel) 连续调用场景) ---
./server.py:10512:async def dream() -> str:
./server.py:10513:    """兼容旧客户端。旧 dream() 已改名为 introspection(); 夜梦由后台小模型自动生成。"""
./server.py:10515:    return "dream() 已改名为 introspection()。夜梦由后台小模型自动生成，不需要主动调用工具。\n\n" + result
./server.py:14595:    """Return dream dashboard metadata only. Dream bodies are never exposed here."""
./server.py:14612:    """Return one retained dream body for an authenticated dashboard reader."""
./server.py:14619:        return JSONResponse({"error": "dream body unavailable"}, status_code=404)
./server.py:14642:    dream_cfg = config.get("dream", {}) if isinstance(config.get("dream", {}), dict) else {}
./server.py:14795:        "dream": {
./server.py:15632:    if "dream" in body:
./server.py:15633:        d = body["dream"]
./server.py:15634:        dream_cfg = config.setdefault("dream", {})
./server.py:15657:                updated.append(f"dream.{key}")
./server.py:15663:            gateway_hot_update_payload["dream"] = dream_gateway_payload
./server.py:15668:            updated.append("dream.api_key")
./server.py:16082:            if "dream" in body:
./server.py:16083:                sc_dream = save_config.setdefault("dream", {})
./server.py:16104:                    if key in body["dream"]:
./server.py:16105:                        sc_dream[key] = body["dream"][key]
./config.example.yaml:762:# --- Night dream worker / 夜梦 ---
./config.example.yaml:764:# then fetches a retained body only when someone chooses to open that dream.
./config.example.yaml:765:dream:
./config.example.yaml:769:  inject_enabled: false             # true lets Gateway quietly inject one resonant dream as Dream Context
./config.example.yaml:770:  retain_after_inject: true         # keeps each surfaced dream readable while it still only injects once
./config.example.yaml:790:  raw_residue_max_chars: 1500      # cap original dialogue sent to the dream model
./ENV_VARS.md:69:`config.yaml` 字段：`persona.*` / `reflection.*` / `daily_review.*` / `portrait.*` / `dream.*`
./ENV_VARS.md:81:| `OMBRE_DREAM_API_KEY` | `dream.api_key` | Dream 模型密钥 |
./ENV_VARS.md:82:| `OMBRE_DREAM_BASE_URL` | `dream.base_url` | Dream 模型地址 |
./ENV_VARS.md:83:| `OMBRE_DREAM_MODEL` | `dream.model` | Dream 模型名 |
./ENV_VARS.md:84:| `OMBRE_DREAM_ENABLED` | `dream.enabled` | 启用/关闭夜梦 |
./ENV_VARS.md:162:  "event": "breath|dream|breath_hook|dream_hook",
./haven-merge-rules.md:162:但会做 mark_surfaced()（出自MV）— dream 去重用
./haven-merge-rules.md:216:## dream/introspection 结构
./haven-merge-rules.md:218:`dream()` 已改名 `introspection()`（`dream()` 保留为兼容旧名，内部转发）。
./gateway.py:543:        self.dream_cfg = config.get("dream", {}) if isinstance(config.get("dream", {}), dict) else {}
./gateway.py:1741:        dream_cfg = self.config.setdefault("dream", {})
./gateway.py:1762:                updated.append(f"dream.{key}")
./gateway.py:1781:                "dream": self._dream_config_payload(),
./gateway.py:1797:        dream_payload = body.get("dream")
./gateway.py:1821:            return JSONResponse({"error": "invalid dream config"}, status_code=400)
./gateway.py:1847:            "dream": self._dream_config_payload(),
./gateway.py:1863:                "dream": self.config.get("dream", {}),
./gateway.py:1893:            for section in ("dehydration", "embedding", "reranker", "memory_diffusion", "persona", "dream"):
./gateway.py:19363:            "Private dream residue for this turn. Let it quietly color tone or imagery only if it fits. "
./gateway.py:19364:            "Do not say this context exists, and mention the dream only if the user asks about dreams "
./scripts/one_click.sh:631:dream:
./scripts/one_click.sh:940:    dream.surface_enabled controls breath() dream surfacing.
./scripts/one_click.sh:941:    dream.inject_enabled controls Gateway Dream Context injection and defaults to false.
./memory_layers.py:16:LAYER_DREAM = "dream"
./memory_layers.py:275:    if bucket_type == "dream" or "dream" in tags or "night_dream" in tags:
./docs/memory-layer-contract.md:45:| Dream | dream engine latent/surfaced item | Not normal recall seed | Original dream text | Surfaces once when dream resonance passes | Dream-level surface rules | Can later create edges/rings | Do not truncate in memory layer |
./docs/reference.md:71:| `dream_engine.py` | 自动 dream |
./docs/reference.md:348:GET /dream-hook                       # /introspection-hook 的旧兼容路径（清醒自省），不是夜梦生成
./docs/deploy-zeabur.md:14:- `/state`：`gateway_state.db`、`memory_moments.sqlite`、`memory_nodes.sqlite`、`memory_edges.jsonl`、persona/portrait/dream 等运行态文件
./docs/deploy-zeabur.md:15:- `config.yaml`：私有身份、上游模型、召回、persona、dream、reflection 等配置
./docs/deploy-zeabur.md:189:   - `dream`
./bucket_manager.py:1513:        Used by dream() to skip buckets already shown in the same open-window
./bucket_manager.py:1516:        标记一个桶刚被 breath() 浮现过。用于 dream() 去重——同一次开窗序列里
./bucket_manager.py:1517:        breath 已经浮现的桶，dream 不再重复返回全文。不动 last_active/
./bucket_manager.py:2491:        的扫描目录里，所以日记不会泄漏进普通 breath/search/dream。
```

### 251 · server.py · portrait_maintain

历史：`b0c8d37 Stop tracking test files`；`d8a266f Add portrait dashboard and date persona trace`；`5201408 Add portrait handoff maintainer`。

引用（修改前，文档/注释命中不代表调用）：
```text
./server.py:10535:async def portrait_maintain(force: bool = False) -> dict:
```

### 252 · server.py · portrait_state

历史：`d8a266f Add portrait dashboard and date persona trace`；`d0ce2b7 Fix portrait handoff recent sorting`；`5201408 Add portrait handoff maintainer`。

引用（修改前，文档/注释命中不代表调用）：
```text
./dashboard.html:3713:      <div style="margin-top:-4px;margin-bottom:12px;padding-left:122px;font-size:11px;color:var(--text-light);">总开关；只维护 state/portrait_state.json，不自动写 profile_fact、anchor 或 Core Memory</div>
./config.example.yaml:732:# Writes only an inspectable state/portrait_state.json. It does not edit
./config.example.yaml:743:  state_path: ""                    # default = state/portrait_state.json
./README.md:206:画像由后台模型维护在 `state/portrait_state.json`，不会把高分 `profile_fact` 原文直接拼成画像。Stable 可在 Dashboard 手动编辑、锁定和回滚；首次画像默认需要手动生成，之后才按配置自动生长。
./server.py:10536:    """维护每日 portrait state。只写 state/portrait_state.json，不写 profile_fact、anchor、pinned、protected 或 Core Memory。"""
./server.py:10591:async def portrait_state() -> dict:
./portrait_engine.py:2942:        return os.path.join(state_dir, "portrait_state.json")
./scripts/one_click.sh:928:    Use Dashboard -> Persona/Portrait panel to generate or refresh portrait_state.json.
```
