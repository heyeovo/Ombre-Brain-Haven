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
10. `tests/test_recall_phase1_shadow.py` 覆盖真实反馈语句、候选独立复审、自然 contextual、关键词单项拒绝、括号动作边界、planner 降级、embedding 状态和身份称呼隔离。

## Phase 1 验收

- Debug 明确记录 recall necessity 的结果与理由；
- planner 正常和 degraded 两条路径可区分；
- shadow 结果不改变正式注入；
- Round 25、54、61 的明确回忆意图不能在 shadow 中被错误 vague / axis 全拒；
- Round 9、12、57 的非明确请求不能因 planner 降级而扩大召回；
- 召回透镜能对比正式与 shadow 结果；
- 对应测试和 Dashboard build 均通过。

最新本地验证结果（2026-08-28）：

- Phase 1 专项测试 19/19 通过；
- Haven 全套测试 139/139 通过；
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

## 第三轮线上验收发现与修正

线上 Shadow 测试 session `ob2-20260827-r1bpf2` 的自然话题“话说 今天下雨了 小言”已正确判为 `contextual`。正确桶“每一场雨都跟你在一起”为 `semantic=0.587 / keyword=0.908 / score=1.000`；边界噪声桶“小言写给小羊的情书”为 `semantic=0.539 / keyword=0.669 / score=0.866`。后者说明语义与关键词同时有中等分仍可能来自同一个泛身份背景，不能视为两份独立相关证据。

已确认产品规则：`言之 / 小言 / 小羊` 等配置中的高频名称和日常称呼属于对话背景，默认不作为 Shadow 主题关键词或 rare-name 独特证据。Gateway 在 Shadow 主题提取前先用边界隔离完整称呼，再重新提取可信主题；因此“下雨”可保留，而称呼及称呼边界产生的异常组合词不能帮助情书桶通过。该修正仍不改变正式拆词、搜索、评分、admission 或注入。

Coolify `haven-brain` 容器执行 `python backfill_embeddings.py --dry-run` 的线上结果为 `Total buckets: 318 / Missing embeddings: 0`。该脚本使用当前 embedding 模型和维度检查桶向量，因此没有缺失或模型/维度过期项，不执行 backfill。召回透镜显示的 `语义 —` 主要表示关键词候选有向量但未进入本轮 semantic Top K；同轮存在其他真实语义分时，可排除整轮语义查询未运行。Dashboard 当前未展示 `semantic_status` 原文，本阶段不做视觉调整。

同一 session 的 Round 12 再次发送“话说 今天下雨了 小言”时，所有候选均无语义分；原始 Debug 已确认 `semantic_status=query_timeout / semantic_score=null`。这证明存量桶向量完整不等于每轮 query 向量都能在时限内返回：Gateway 当前对 query 语义搜索的等待上限为 3 秒，本轮在该阶段超时。此次身份称呼修正没有改动语义调用链；两者只是同时出现在发布后验收中。

已补 Shadow-only 安全降级：仅 `query_timeout / query_failed / query_embedding_unavailable / query_embedding_failed` 可触发；仅保留正式结果已有候选；候选还必须命中隔离称呼后的可信主题词且 `keyword >= 0.85`。因此雨桶可由“下雨 + 0.908”保留，情书桶不能靠称呼或 `0.669` 通过；additional/suppressed 候选不会因此新增，`indexed_not_in_semantic_top_k` 也不会触发。正式 3 秒超时、重试策略、正式 admission 和排序均未修改，继续收集 query timeout 频率后再决定是否单独调整可靠性。

同一 session 的 Round 15 进一步确认：语义查询恢复后，雨桶与情书桶都被 Shadow 加入，因此问题不在 timeout fallback。根因是 Shadow 没有把“小言/小羊”稳定隔离为中性称呼，同时 query 词命中桶标题和复合 rare-name 可绕过可信主题要求。现已要求 rare-name、身份名候选和标题直接命中同时具有清理后的可信主题；只有明确桶 ID 仍可直接放行。正式召回路径保持不变。

部署后新产生的 Round 19 仍选中情书桶。原始 Debug 证明不是长正文偶然命中“下雨”，而是线上持久化 `/config/config.yaml` 仍使用模板身份 `AI / User / 用户 / 对方`：`ignored_identity_terms=[]`、`topic_terms=["下雨","小言"]`、`matched_topic_terms=["小言"]`、`shadow_keyword_score=0.6694`，最终由 `shadow_semantic_keyword_agreement` 放行。为避免把个人称呼写死在代码或扩充多组身份环境变量，Coolify compose 现只透传一个逗号分隔变量 `OMBRE_RECALL_IGNORED_ADDRESS_TERMS`；Gateway 将它与已配置身份称呼合并，仅用于 Shadow 主题清理。建议线上先设为 `小言,言之,小羊`，以后可直接追加称呼。Debug 新增 `ignored_address_terms` 和 `ignored_configured_address_terms`；正式召回不受影响。

首次上线该变量后，情书桶被正确移除，但雨桶也被 Shadow 错误移除。原因是曾用 `_calc_topic_score(query, bucket)` 对单桶重算清理后的关键词分；该函数内部 BM25 依赖候选语料，单桶分数不能与正式候选池的阈值比较。现已删除第二套单桶关键词重算：Shadow 保留正式同口径关键词分，称呼仅不能建立可信主题。固定回归为雨桶 `下雨 + semantic 0.588 + keyword 0.9083` 通过、情书桶只命中称呼而拒绝。

已确认但本次暂不实现的后续边界：配置中的称呼应“默认中性”，不能无条件永久删除。当称呼本身是明确讨论对象，例如“你还记得第一次叫我老婆吗”，需要将“老婆”恢复为目标主题；日常句首/句尾呼唤仍不作为证据。该语境区分应单独实现和验收，不与本次恢复正确雨桶混做。

最终修正已由用户 commit/push/deploy，线上完整 SHA 为 `4b2ffd2eba884c6d3bdd1be07dd60a5ba48ceb48`，Coolify 已配置 `OMBRE_RECALL_IGNORED_ADDRESS_TERMS=小言,言之,小羊`。session `ob2-20260827-r1bpf2` Round 25 重放“话说 今天下雨了 小言”后用户确认结果正确：Shadow 只保留雨桶，情书桶不再进入 Shadow 会选。该轮证明称呼配置、可信主题约束和正式关键词分复用已按预期组合；历史 Round 不会自动重算。

## 召回透镜完善状态（2026-08-28 Dashboard 本地完成）

`ob-dashboard2` 已在本地完成单页 Debug 信息补齐，未修改 Haven、召回算法、API route 或 Gateway 原始 Debug：

- `app/recall-lens/page.tsx` 已按 bucket ID 对齐正式候选、Shadow selected 和 Shadow rejected，不再用 `shadow_admission_reason` 是否存在推断选择状态；候选卡明确分开显示正式判断与 Shadow 判断。
- 候选卡直接显示 `semantic_status` 中文说明与原始码；现有折叠详情已接入 `topic_terms`、`matched_topic_terms`、各类 ignored terms、直接证据和 query 语义故障降级字段。
- 轮次层已显示 necessity 原因、targetable、context available、planner status、fallback strategy，以及正式/Shadow 结果和新增/移除。
- `app/recall-lens/recallReasonCopy.ts` 已审计并补齐当前正式/Shadow admission reason、evidence label、semantic status、planner status、fallback strategy 和动态 Planner 错误；`retrieval_alias_only` / `retrieval_alias` 已有准确中文说明。
- 未知内部码的标题会直接显示具体码，并按 Shadow、Planner、语义或一般召回规则提供可理解兜底，不再只显示“尚未收录中文说明”。
- 新增 `tests/recall-reason-copy.test.ts` 固定审计码表、retrieval alias、`query_timeout`、planner/fallback 和未知码兜底。

本地验证：新增测试 5/5 通过；目标文件 ESLint 通过；`npm run build` 通过。完整 `npm test` 为 186 passed / 1 skipped / 2 failed，两个失败分别位于既有 automation proposal 字段契约和 selfhost runtime message 断言，与召回透镜改动文件无关，未越界处理。浏览器本地页可启动，但未代填 Dashboard 登录口令，因此 Round 25、语义超时轮次及未知码展示仍需用户 commit/push/deploy 后在已登录页面做最终真实数据验收。

## 发布后仍需继续核查

1. **Embedding 内容新鲜度**：线上 318 个桶已确认没有缺失或模型/维度过期向量，因此不执行 backfill。现有检查不包含正文内容哈希；只有后续出现“桶正文已改但向量未刷新”的具体证据时，再单独审计内容新鲜度。
2. **Semantic 查询状态**：新记录已能区分 `scored`、`indexed_not_in_semantic_top_k`、`query_embedding_unavailable/failed`、`query_timeout/failed` 和 engine disabled；下一窗口把这些已有字段完整呈现在召回透镜，不做视觉重构。
3. **Session 去重真实契约**：用户预期“同一窗口已召回桶不会再次召回”，但当前代码默认 `skip_recent_rounds=5`，且强证据存在 bypass。发布后先用实际 session 验证；在结论明确前，不依赖“全窗口绝不重复”作为放宽 explicit 的唯一安全条件，也不在本 Phase 顺手改去重。
4. **Planner 实际可用性**：按新记录统计 normal / not_triggered / disabled / degraded，以及 dehydration 鉴权错误；确认用户当前线上是否确有可用 dehydration 配置。Planner 不可用时 contextual 必须保持“不新增”。
5. **Shadow relevance 泛化**：继续收集自然话题、明确过去指向、系统复盘和 keyword-only 噪声案例。重点观察 `semantic >= 0.50 + keyword >= 0.65 + specific topic` 的第一版组合证据是否放过无关桶或漏掉真正相关桶；该阈值只用于 Shadow，线上验收后再决定是否调整。
6. **缺少目标桶的明确请求**：旧 Round 25、54 若对应桶当前不存在，无法证明“明确请求不会整轮误杀”；必须另找已有真实桶的 explicit 案例替代，不把“候选不存在”误判为 gate 失败。

## 后续推进顺序

1. Dashboard 召回透镜的单页 Debug 信息与中文映射已在本地完成；由用户 commit/push 并按 Dashboard 现有 Coolify 流程部署后做真实页面验收。
2. 部署后先用 `ob2-20260827-r1bpf2` Round 25 验证雨桶 Shadow 选择、情书桶 Shadow 拒绝及双方主题证据，再核对 Round 12 的 `query_timeout`；随后继续重放原固定案例及 `ob2-20260827-zoazvn` Round 4、6、9、16、20、21、23、24，直接在召回透镜记录结论。
3. 继续验收 necessity、候选独立审核、planner 降级不扩召回和 `semantic_status`；embedding 覆盖统计已完成，不做 backfill。
4. 称呼作为明确讨论对象的语境区分另开后续窗口，不与召回透镜修改混做。
5. 只有 Shadow 验收稳定后才进入 Phase 2，把统一 relevance/admission 渐进接入正式召回；仍不在 Phase 2 顺手处理家族聚类、关系边或额外 LLM agent。

Haven Phase 1 当前修正已全部 commit/push/deploy；最新线上 SHA 和 Round 25 验收见上。下一窗口的 Dashboard 修改完成并部署后，除原固定 Round 9、12、25、54、57、61 外，重点重放 `ob2-20260827-zoazvn` 的 Round 4、6、9、16、20、21、23、24。验收 Shadow 是否删除 keyword-only 噪声、自然 contextual 是否只选高相关桶，以及候选 `semantic_status` 是否能解释原来的 0 分。历史 Debug 不会自动补算新字段。未完成线上真实验收前，不进入 Phase 2 正式 gate 切换。

## 下一窗口唯一范围

召回透镜代码已本地完成。下一窗口若继续本议题，只做 Dashboard 部署后的真实页面验收：先检查 `ob2-20260827-r1bpf2` Round 25 和 Round 12，确认不打开 Gateway Debug 也能解释 Shadow 选择/拒绝、主题证据和 `query_timeout`；再按上述固定轮次继续验收。若发现纯展示缺口，只修 `ob-dashboard2/app/recall-lens/`；不得扩散到 Haven 召回算法、称呼语境判断、正式 admission gate 或 Gateway 原始 Debug。

## 不得扩散的边界

- Phase 1 不直接切换正式召回路径，只新增可观测的 shadow 结果。
- Phase 1 不创建家族表、关系边或自动聚类任务。
- 下一窗口只补召回透镜诊断字段、折叠详情和中文映射，不重做整体视觉，不修改 Gateway 原始 Debug 页面。
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
