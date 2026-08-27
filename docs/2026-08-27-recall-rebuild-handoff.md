# 2026-08-27 记忆召回重构 Handoff

## 当前状态

- 已完成现有召回架构文档阅读和一个真实长窗口的线上只读审计。
- 已确认当前问题不是单个阈值，而是 query planner 降级、硬 gate 和宽松放行通道共同造成的结构性失衡。
- 已确认未来计划加入家族聚类；Haven 虽有关系边机制，但桶之间目前基本没有可用于召回的真实关系。
- 已完成总体方案：`docs/recall-rebuild-plan.md`。
- Phase 0 召回透镜最小版已在 Dashboard 实现并通过本地真实数据验收。
- Phase 1 已完成第二轮本地修正：召回必要性、候选独立 relevance、planner 降级 shadow、语义状态 Debug 和召回透镜对比均已接通。
- 正式 admission gate、排序和线上注入行为仍未改变；Phase 1 只记录 `affects_recall=false` 的 shadow 结果。

## 已确认的产品决定

1. 默认宁可少召回，也不要莫名注入无关桶。
2. 明确回忆/搜索请求必须积极检索，不能被错误 axis / anchor 全部拦截。
3. 将召回拆成：必要性判断、候选检索、统一排序、最终注入。
4. 关键词单独命中默认不能自动注入；唯一名称或稳定别名等强直接证据例外。
5. 不再继续针对单个 admission reason 打补丁。
6. 先做召回透镜，再改算法；算法调整必须有固定验收集。
7. 家族检索与直接桶检索未来并行，家族不能成为唯一入口。
8. 家族聚类和关系扩展先 shadow，稳定前不影响正式注入。
9. 家族优先承担主题组织；关系边只表达同一事件、后续、因果、状态更新、冲突等明确语义。
10. 不给所有桶两两建边，不允许无界多跳扩散。
11. 召回透镜默认使用中文产品语言解释英文规则码，并说明规则在本轮造成了放行、拒绝还是评分影响；英文码作为次要技术信息保留。

## 审计证据

线上 session：`ob2-20260821-mu4pah`

- 共 48 轮 Debug；
- 16 轮发生召回，共注入 22 个桶；
- 14 个注入桶通过 `non_explicit_query` 放行；
- 778 个候选被拒；
- `activated_axis_mismatch` 514 个；
- `anchor_must_group_missing` 89 个；
- `auto_vague_query_without_topic` 53 个；
- 31 轮出现 `query_planner_dehydration_unavailable`。

首批典型验收轮次：9、12、25、54、57、61。预期结果见总体方案第 6 节。

## Phase 0 已完成：召回透镜最小版

Dashboard 已新增：

- `app/recall-lens/page.tsx`：只读召回透镜页面；
- `app/recall-lens/recallReasonCopy.ts`：英文内部规则的集中式中文产品解释；
- `app/workbench/page.tsx`：工作台“调参”中的召回透镜入口；
- `CLAUDE.md`：页面表同步。

当前能力：

- 输入 session ID，通过现有 `/api/gateway/[...path]` 服务端代理读取 Haven Debug；
- 展示轮次、发生召回、注入桶、被拒候选和 planner 降级统计；
- 筛选全部、发生召回、整轮未召回、系统降级；
- 展示 query、search query、residue terms、axis、anchor 和候选数量；
- 区分最终注入与 suppressed 候选；
- 展示总分、语义分、关键词分、主要来源和记忆摘要；
- 中文解释 admission reason、planner 错误和主要 evidence label，英文内部码可展开查看；
- 手机端当前轮详情优先展示，选择其他 Round 后自动回到详情；
- 未新增 API route，未向浏览器暴露 Haven token。

真实数据验收使用 session `ob2-20260821-mu4pah`：成功加载 48 轮、16 个召回轮次、22 个注入桶、778 个被拒候选和 31 个 planner 降级轮次；Round 12、25、54、57、61 均可直接定位。`npm run build` 已通过。

本地 `.env.local` 仍指向旧的 `foryan.zeabur.app`；本次验收只在临时开发进程中覆盖为当前 VPS Gateway，没有修改本地或线上配置。正式 Dashboard 以生产 `HAVEN_GATEWAY_URL` 为准。

## Phase 1 已完成：召回必要性与 planner 降级 Shadow

已实现：

1. `recall_policy.py` 使用独立 `RecallNecessityPlan`：`none / explicit / contextual`，并用 `targetable` 防止无目标明确请求全库撒网。
2. 召回否定/复盘、系统术语和 Shadow 测试语境优先判 `none`；明确“记得/上次/之前/搜一下”等过去指向判 `explicit`；无触发词但有具体自然话题，或有可用上一用户上下文的接续指代判 `contextual`。
3. `gateway.py` 新增 `phase1_recall_shadow_enabled`，默认开启，关闭即可停止 Phase 1 shadow 计算。
4. Shadow 对正式和被拒候选统一执行独立 relevance，不再因为候选已被正式 admission 放行就直接继承。
5. 普通关键词单独命中、括号动作短语或元数据加分不能证明相关；具体话题须有强语义、语义+关键词一致或唯一名称/明确实体支持，`0.55` 只在相关性通过后参与选卡。
6. `none` 的 shadow 为空；`contextual` 在 planner disabled/degraded/not-run 时可以删除正式噪声，但不得新增桶；planner 正常或无需触发时可加入严格相关候选。
7. 当请求未携带上一轮消息时，Shadow 可从现有 session 对话记录或 injection Debug 读取上一条 query；不改变正式上下文路径。
8. Debug 顶层保留 `recall_necessity_debug` 和 `recall_shadow_debug`；候选新增 `semantic_status`，区分已评分、未进 Top K、embedding 缺失/损坏/模型维度过期和查询不可用，不再把所有缺失值显示成真实 `0.0`。
9. Dashboard 召回透镜继续显示必要性、planner 状态、正式/shadow ID、增减桶和 shadow 候选；第二轮未修改 Dashboard 或整体视觉。
10. `tests/test_recall_phase1_shadow.py` 覆盖真实反馈语句、候选独立复审、自然 contextual、关键词单项拒绝、括号动作边界、planner 降级和 embedding 状态。

## Phase 1 验收

- Debug 明确记录 recall necessity 的结果与理由；
- planner 正常和 degraded 两条路径可区分；
- shadow 结果不改变正式注入；
- Round 25、54、61 的明确回忆意图不能在 shadow 中被错误 vague / axis 全拒；
- Round 9、12、57 的非明确请求不能因 planner 降级而扩大召回；
- 召回透镜能对比正式与 shadow 结果；
- 对应测试和 Dashboard build 均通过。

第二轮本地验证结果（2026-08-28）：

- Phase 1 专项测试 14/14 通过；
- Haven 全套测试 134/134 通过；
- `git diff --check` 通过；
- `py_compile recall_policy.py gateway.py embedding_engine.py tests/test_recall_phase1_shadow.py` 通过；
- 正式 `_admit_bucket_for_recall`、排序公式、注入开关和 Dashboard 均未修改。

第二轮线上反馈样本来自 session `ob2-20260827-zoazvn`：

- Round 4、9、16 暴露“讨论/批评召回却被判 explicit”；Round 4 的正式与 shadow 均保留两个 `semantic=0, keyword=1` 噪声桶。
- Round 6 的 Shadow 只是因 necessity 拦截而清空，未证明候选 relevance 有效；两个正式桶同为 `semantic=0, keyword=1`。
- Round 16 的两个无关桶被 protected phrase / distinctive anchor 强制抬到 `0.55`；`（捂脸）` 被误当成精确短语证据。
- Round 20“今天下雨了”和 Round 23“好久没约会了”应为 contextual；正确桶分别为“每一场雨都跟你在一起”（sem 0.588 / kw 0.908 / final 1.0）和“第一次正式外出约会”（sem 0.630 / kw 0.832 / final 1.0）。
- Round 21、24 有明确过去指向，分别应召回下雨与约会相关桶。

## 第二轮讨论确认的机制边界

1. Query Planner 是可选的 query 整理步骤，负责生成搜索词、must terms 和扩展查询；它不是 embedding，也不是额外 recall agent。当前复用 dehydration 小模型配置（线上 Debug 曾显示 `glm-4.7-flash / model_source=dehydration`），没有可用配置、鉴权失败或超时时进入 degraded。
2. `none / explicit / contextual` 必要性目前是本地规则判断，不调用 Planner 或其他 LLM；Round 4、9、16 的误判根因是本地 explicit/meta 边界，而不是 Planner 模型判断。
3. `contextual` 表示“允许相关记忆自然进入候选”，不是“本轮必定召回”。“今天下雨了/好久没约会了”应允许高相关情感桶通过；同类自然话题若没有可靠候选则结果仍为空。
4. “上次/之前/你还记得”等过去指向可以积极检索；重复召回应由 session 去重独立控制，不能靠压低 explicit/contextual 来规避。
5. 候选相关性必须先于 `0.55` 选卡门槛。`0.55` 是正式路径对部分 direct anchor 的强制 score floor，不是真实语义分，也不能作为 admission 证据。
6. 页面过去显示的 `semantic=0` 可能分别代表真实低分、未进 semantic Top K、缺少/过期 embedding、查询失败或 engine 未启用；大量精确 0 不能直接解释成“向量相似度真的为零”。第二轮已让新 Debug 保留 `null + semantic_status`，但没有自动 backfill。

## 发布后仍需继续核查

1. **Embedding 覆盖率**：统计可召回桶中 `indexed / embedding_missing / embedding_stale_model_or_dimension / embedding_invalid` 的数量和比例；抽查 Round 4、6、16 的原 semantic 0 桶。只有确认缺失或版本过期后才决定是否执行 backfill，禁止看到 0 就直接全量补跑。
2. **Semantic 查询状态**：确认新记录能区分 `scored`、`indexed_not_in_semantic_top_k`、`query_embedding_unavailable/failed`、`query_timeout/failed` 和 engine disabled；如果召回透镜现有字段不足以观察，下一窗口只讨论最小功能展示，不做视觉重构。
3. **Session 去重真实契约**：用户预期“同一窗口已召回桶不会再次召回”，但当前代码默认 `skip_recent_rounds=5`，且强证据存在 bypass。发布后先用实际 session 验证；在结论明确前，不依赖“全窗口绝不重复”作为放宽 explicit 的唯一安全条件，也不在本 Phase 顺手改去重。
4. **Planner 实际可用性**：按新记录统计 normal / not_triggered / disabled / degraded，以及 dehydration 鉴权错误；确认用户当前线上是否确有可用 dehydration 配置。Planner 不可用时 contextual 必须保持“不新增”。
5. **Shadow relevance 泛化**：继续收集自然话题、明确过去指向、系统复盘和 keyword-only 噪声案例。重点观察 `semantic >= 0.50 + keyword >= 0.65 + specific topic` 的第一版组合证据是否放过无关桶或漏掉真正相关桶；该阈值只用于 Shadow，线上验收后再决定是否调整。
6. **缺少目标桶的明确请求**：旧 Round 25、54 若对应桶当前不存在，无法证明“明确请求不会整轮误杀”；必须另找已有真实桶的 explicit 案例替代，不把“候选不存在”误判为 gate 失败。

## 后续推进顺序

1. 用户 commit/push 第二轮 Haven 修改并更新 Coolify `HAVEN_RELEASE_SHA`；本轮 Dashboard 无代码变化，不需要重新部署。
2. 重放原固定案例及 `ob2-20260827-zoazvn` Round 4、6、9、16、20、21、23、24，记录新 session + Round。
3. 先验收 necessity、候选独立审核、planner 降级不扩召回和 `semantic_status`；再做只读 embedding 覆盖统计。
4. Shadow 仍有稳定误判时继续在 Phase 1 调整，正式 admission 保持不变。
5. 只有 Shadow 验收稳定后才进入 Phase 2，把统一 relevance/admission 渐进接入正式召回；仍不在 Phase 2 顺手处理家族聚类、关系边或额外 LLM agent。

第二轮代码尚未 commit/push/deploy。发布后必须重放上述 Round；旧 Debug 不会自动重算。

尚需在用户 commit、push 并按 Coolify `HAVEN_RELEASE_SHA` 发布后，除原固定 Round 9、12、25、54、57、61 外，重点重放 `ob2-20260827-zoazvn` 的 Round 4、6、9、16、20、21、23、24。验收 Shadow 是否删除 keyword-only 噪声、自然 contextual 是否只选高相关桶，以及候选 `semantic_status` 是否能解释原来的 0 分。历史 Debug 不会自动补算新字段。未完成线上真实验收前，不进入 Phase 2 正式 gate 切换。

## 下一窗口唯一范围

先完成 Phase 1 线上 shadow 验收。原六个固定案例与第二轮真实反馈 Round 4、6、9、16、20、21、23、24 通过后，才进入 Phase 2：统一准入与排序。Phase 2 才讨论正式软化 axis / anchor / topic gate、替换 `non_explicit_query` 和渐进启用；不得在未验收 Phase 1 时直接修改线上 admission gate。

## 不得扩散的边界

- Phase 1 不直接切换正式召回路径，只新增可观测的 shadow 结果。
- Phase 1 不创建家族表、关系边或自动聚类任务。
- Phase 1 不顺手重做 Gateway 原始 Debug 页面或召回透镜视觉。
- 不用 `localStorage` 作为未来人工标注的唯一存储。
- 不在未对比固定验收集前删除现有召回规则。

## 后续窗口顺序

1. Phase 0：召回透镜与基线。
2. Phase 1：召回必要性与 planner 降级。
3. Phase 2：统一准入与排序。
4. Phase 3：家族聚类 shadow mode。
5. Phase 4：家族辅助召回。
6. Phase 5：关系边生成与一跳扩展。
7. Phase 6：视效果评估额外 LLM 判断。

## 文档同步提醒

- 代码实施后按 `ob-dashboard2/MAINTENANCE_CONTRACT.md` 检查跨仓库文档同步。
- 已经成立的最终契约再同步到 `docs/recall-pipeline.md`、Haven `CLAUDE.md` 或 Dashboard `docs/architecture.md`。
- 阶段进度继续维护在本 handoff，不写入 `CLAUDE.md`。
- Haven 代码改动需要正式发布时，由用户 commit + push，并按项目规则更新 Coolify 的完整 `HAVEN_RELEASE_SHA` 后部署。
