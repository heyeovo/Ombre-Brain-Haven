# 2026-08-27 记忆召回重构 Handoff

## 当前状态

- 已完成现有召回架构文档阅读和一个真实长窗口的线上只读审计。
- 已确认当前问题不是单个阈值，而是 query planner 降级、硬 gate 和宽松放行通道共同造成的结构性失衡。
- 已确认未来计划加入家族聚类；Haven 虽有关系边机制，但桶之间目前基本没有可用于召回的真实关系。
- 已完成总体方案：`docs/recall-rebuild-plan.md`。
- Phase 0 召回透镜最小版已在 Dashboard 实现并通过本地真实数据验收。
- Phase 1 的召回必要性、候选独立 relevance、planner 降级、utility 和召回透镜线上验收均已完成。
- 2026-09-07 已在本地完成 Phase 2：重构 relevance + utility 默认接管正式桶结果，保留 `legacy` 紧急回滚；待用户 commit/push/deploy 后做线上 smoke。

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

本地验证：新增测试 5/5 通过；目标文件 ESLint 通过；`npm run build` 通过。完整 `npm test` 为 186 passed / 1 skipped / 2 failed，两个失败分别位于既有 automation proposal 字段契约和 selfhost runtime message 断言，与召回透镜改动文件无关，未越界处理。

部署后用户已在召回透镜完成第一组真实数据验收：`ob2-20260827-r1bpf2` Round 25 中，雨桶正式因 `semantic_only` 拒绝、Shadow 因 `shadow_semantic_keyword_agreement` 选择，`topic_terms/matched_topic_terms=下雨`、`ignored_address_terms=小言`；情书桶正式因 `retrieval_alias_only` 拒绝、Shadow 因 `shadow_query_topic_missing` 拒绝，清理后主题为“下雨”、桶未命中可信主题、直接证据均为否。Round 12 已直接显示“查询语义超时（query_timeout）”；该历史记录未保存后续新增的两个 query fallback 布尔字段，页面正确显示“未记录”，不误写成否。情书桶评分证据中的 `retrieval_alias` 已准确显示“命中稳定检索别名”，并说明别名不能单独决定正式注入。第一组固定验收已可完全在召回透镜完成，无需打开 Gateway Debug 或复制 JSON。

后续线上 smoke session `ob2-20260828-i4tso3` 暴露新的 necessity 边界：首轮“这是测试记忆召回的窗口……在观测台看召回情况……不用搜东西”仍被判为 `contextual / natural_contextual_topic`，正式与 Shadow 都因“第一个/窗口”等表面主题保留候选。这是明确错误：召回测试语境、观测召回和否定搜索指令应以高优先级判 `none`，不能继续进入自然话题判断；修复方向应是意图组合与优先级，不是把“第一个/窗口”加入普通停用词。同 session 的“现在其实还是会召回一些不相关的桶”正确判 `none` 并未进入候选阶段。“就是要趁周末快点收尾”与“pro之后第一个周末一起干活”主题确实相关，但用户未期待主动召回，记录为“相关但未必值得此刻翻出”的灰区，不直接视为候选 relevance 错误。

## Phase 1 组合意图优先级修复（2026-08-29 已上线验收）

- `recall_policy.py` 新增高优先级组合意图：同一轮同时表达召回测试、召回观测和“不用搜/不用回忆”时，在 meta、explicit 与 `natural_contextual_topic` 之前返回 `none / targetable=false`，原因码为 `recall_test_observation_search_negated`。
- 三类信号必须同时成立；没有否定搜索与观测目的时，“你还记得我们第一次测试记忆召回的窗口吗”仍为 `explicit / targetable=true`。没有把“第一个、窗口、测试”等普通词加入全局停用表。
- Gateway 原有 `necessity=none` 快速路径保持不变：不调用 `_shadow_candidate_relevance`，Shadow selected/rejected 均为空；正式 admission、排序、注入、relevance 阈值和 recall utility 均未修改。
- `tests/test_recall_phase1_shadow.py` 已加入失败 session 原句、独立原因码、Shadow 不进入候选审核和真正过去事件不误杀的回归。
- 本地验证：Phase 1 专项测试 21/21 通过；Haven 全套测试 142/142 通过；`py_compile recall_policy.py gateway.py embedding_engine.py tests/test_recall_phase1_shadow.py` 通过；`git diff --check` 通过。
- 用户已 commit/push，并以完整 SHA `59a49ad8f5aaca332ef747ed1407e346949333c1` 通过 Coolify 发布。新 session 重放失败原句后，necessity 组合意图结果符合预期；后续发现的 source-record 候选误召回属于下述独立精细度边界，不影响本次 necessity 修复验收结论。历史 Debug 不会重算。

## 已接受保留：source-record 标题局部命中的精细度边界

组合意图修复上线验收时，原句“你还记得我们第一次测试记忆召回的窗口吗”已正确判为 `explicit / targetable=true`，但候选【第一次说爱你】仍被正式与 Shadow 同时召回。该候选为 `score=0.964 / semantic=0.425 / keyword=0.763`；Shadow 原因为 `shadow_unique_direct_evidence`。

召回透镜证据：

- `raw_topic_terms/topic_terms/matched_topic_terms` 均为“第一次、窗口”；两个词在该桶正文中都确实存在且不止一次，不是单关键词误命中。
- `explicit_bucket_id=false`、`rare_name_direct=false`、`identity_name_direct=false`、`exact_direct=false`。
- `_source_record_explicit_bucket_match_reason()` 因“第一次”是标题【第一次说爱你】中长度不少于 3 的子串而返回 `explicit_bucket_title`；只要整桶正文再命中任意可信主题，当前 Shadow 就令 `source_record_direct=true`，进而 `unique_direct=true` 并绕过后续语义门槛。
- Haven 当前产品数据基本遵守“一桶一个事件”，因此未来无需为了此问题增加“同一局部片段共现”约束；整桶正文匹配仍可继续使用。问题在于标题局部词被错误升级成唯一证据，而不是关键词是否位于同一句。

当前决定以稳定跑通为先，本阶段接受保留，不继续修改：

- 该问题属于候选/source-record 直接证据精细度，不属于 necessity 组合意图，也不与 recall utility、阈值、正式 admission 或家族聚类混做。
- 不为【第一次说爱你】或“第一次、窗口、测试”等词写特例，不加入全局停用词。
- 不在当前阶段引入全库短语索引、动态独特性统计或局部片段匹配；也不为了这个案例恢复“测试/记忆/召回”为通用主题词，因为这些词在大量请求中只是召回操作外壳，可能扩大系统类误召回。

未来做精细优化时按由简到繁的顺序评估：

1. 先把“唯一直接证据”收紧为明确桶 ID、完整桶标题和稳定别名；标题局部词只作普通辅助证据，继续走语义与关键词一致性审核。此方案是全局规则，不针对单桶。
2. 重点回归真正由独特标题片段指向的正确案例，例如“你还记得一点半见那次吗”对应【一点半见后来成为习惯】；风险是局部片段未被 rare-name 识别且语义低于门槛时可能漏召回。
3. 只有真实漏召回证明简单规则过严时，再考虑缓存式“短语 → bucket ID 集合”统计：桶新增/修改/合并/删除时更新，召回时只查当前 query 的少量连续短语，不每轮重扫全库。完整且全库独特的连续短语可作为强证据；“第一次、窗口”这类分散普通词即使共同只命中一个桶，也不自动成为唯一证据。
4. 正式与 Shadow 本次都召回了错误桶。未来实施时必须分别核对 shared source-record 生成路径与 Shadow `unique_direct`，先在 Shadow 固定验收，再决定何时影响正式路径。

## 轻量 Recall Utility Shadow（2026-08-30 本地完成）

已确认产品方向：自然 `contextual` 是 Haven 日常主动召回的主入口，不能因本地规则
无法证明高价值就默认沉默。Recall utility 只审核已经通过候选 relevance 的桶，第一版
采用三档契约：

- `promote`：代码有明确正向依据；当前包括用户明确期待回忆，以及有可用上一轮用户
  上下文的接续指代；
- `neutral`：候选相关，但本地规则无法可靠判断增量价值；仍保留召回资格；
- `reject`：代码能确定没有增量；当前只覆盖候选正文与用户原句完全相同的保守边界。

已在 Haven 本地实现：

- `recall_policy.py` 新增 `RecallUtilityDecision`，将三档结果与是否仍有资格的契约固定；
- `gateway.py` 在 `_shadow_candidate_relevance` 通过后执行 Shadow-only utility；
- utility 优先从 `promote` 候选选择，没有 promote 时继续从 neutral 候选选择；
- Shadow 最终投影暂时最多一张卡，正式 `inject_max_cards`、正式 admission、正式排序和
  实际注入完全不变；
- `recall_shadow_debug` 新增 `utility_contract`、`shadow_max_cards`、
  `utility_candidates`，选中或 utility 拒绝候选保存 `shadow_utility` 详情；
- `_select_dynamic_buckets` 把已有上一轮用户消息传给 utility，不新增会话读取路径。

固定案例当前预期：明确回忆和“那后来呢 + 可用上一轮上下文”为 promote；“今天下雨了”、
“好久没约会了”、“就是要趁周末快点收尾”、“pro 之后第一个周末一起干活”对应的
相关候选先为 neutral 且仍可入选；完全重复原句的候选为 reject。后续若要判断更细腻的
关系连续性，可在保持三档接口和硬边界不变的前提下加入轻量模型；模型失败时应回退
neutral，不能扩大 relevance 未通过的候选池。

本地验证：Phase 1/utility 专项测试 26/26 通过；Haven 全套 unittest 147/147 通过；
`py_compile recall_policy.py gateway.py tests/test_recall_phase1_shadow.py` 与
`git diff --check` 通过。为运行测试，在仓库忽略的 `.venv` 安装了 `requirements.txt`，
不进入提交。本次尚未 commit/push/deploy，线上完整 SHA 仍为
`59a49ad8f5aaca332ef747ed1407e346949333c1`。

Haven utility 已由用户 commit/push，完整 SHA 为
`2fa725a834af1b040c9d7bc77379b99b2b44b2a6`；仍需以 Coolify 实际采用该 SHA 且
Brain/Gateway 均 healthy 为线上生效标准。

## Recall Utility 召回透镜展示（2026-08-30 Dashboard 本地完成）

用户再次确认：所有召回验收必须在 Dashboard 召回透镜完成，不能依赖 Gateway Debug；
后者数据过长，浏览器容易停止响应。`ob-dashboard2` 已完成：

- “新规则 Shadow 对比”直接展示 utility contract、Shadow 单卡上限和全部
  `utility_candidates`；
- 三档使用中文产品语言显示为“优先召回 / 保留召回资格 / 不值得本轮召回”，同时保留
  `promote / neutral / reject` 原始码；
- 每张候选卡新增“召回价值 Utility”，显示原因、前文是否可用、命中主题和判断器；
- 没有 utility 的候选明确区分“未通过 relevance，未进入 utility”和“旧记录没有
  utility 数据”，不把字段缺失误判为 neutral/reject；
- 新增 `shadow_utility_rejected` 与五个 utility 原因码中文解释；未知码继续保留原始值。

Dashboard 本地验证：utility 中文映射测试 6/6 通过；目标文件 ESLint 通过；
`npm run build` 通过。浏览器本地检查被 Dashboard 登录口令拦截，未代填用户凭据；
部署后视觉与真实数据验收仍须使用已登录页面和新 session。本次未修改其他页面、API、
Gateway Debug 或正式召回。

Dashboard 与 Haven 后续均已发布，并由用户通过召回透镜截图完成 Utility MVP 真实验收：

- Round 1 的召回测试语境并明确“不要搜”，正确判为不召回；
- Round 2“今天下雨了”找到相关雨桶，Utility 为 `neutral`，Shadow 允许进入；
- Round 3“好久没约会了”找到两个合理的 `neutral` 候选，最终只选择一张卡；
- Round 4 明确回忆请求判为 `promote`，并选中“第一次正式外出约会”；
- Round 8“趁周末快点收尾”没有可靠候选进入最终召回，符合保守基线。

因此 Utility MVP 判定通过，基础版暂时没有需要立刻修改的问题。已接受保留两个观察项：
多个 `neutral` 候选之间仍主要依赖 relevance 排序；明确回忆会把所有通过 relevance 的
候选升为 `promote`，该轮曾出现 11 个 promote，但最终排序选桶正确。

真实数据尚未覆盖“那后来呢”一类上下文指代型 `promote`，以及候选正文与用户原句完全
相同的 `reject`。其中上下文指代是进入 Phase 2 讨论前建议补做的短测；原样重复已有单元
测试，不作为阻塞项。当前上下文实现只识别指代标记、确认上一轮用户消息存在，并将已经
通过 relevance 的候选升为 promote；尚未把前文事件合并进候选检索，也不能判断候选是否
真正回答了“后来发生什么”。

## Phase 2 本地完成：重构规则正式接管（2026-09-07）

- `gateway.py` 新增 `OMBRE_RECALL_DECISION_MODE`，默认 `rebuilt`；重构 relevance + utility 的单卡投影成为正式桶结果。设为 `legacy` 可紧急恢复旧结果，旧代码暂不删除。
- Debug 保留三组事实：`legacy_bucket_ids`、`shadow_bucket_ids`、`effective_bucket_ids`；重构接管时 `affects_recall=true`。
- auto-vague 的明确请求可采用重构投影；planner disabled/degraded/not-run 的 contextual 仍保持保守不扩张。
- 旧 source-record 后置追加在 rebuilt 模式停用，不能绕过 Utility 或最多一张桶卡的上限。
- Dashboard 完整窗口排除契约保持不变：已返回桶与本窗口真正新建的 hold 桶由 `exclude_ids` 先从候选池移除，再在最终出卡前硬过滤，不会返回或抢占单卡位。
- Hook 返回的桶卡可在正文下附一行显式关联桶名称与 ID，按关系边 confidence 排序，最多两个；不附关联正文，不使用语义相似猜测关系。
- Hook 每轮重复的英文说明和 Dashboard 动态中文说明已移除；等价使用规则进入 CC/selfhost 共用的稳定 persona system prompt。动态部分只保留边界标签与本轮正文。
- Dashboard 召回按钮和详情改为“约 N token”。外层表示完整注入，内层表示卡片/日期正文，因此数值可以不同但含义明确；灰色弹窗说明仍只在前端显示，不进入 prompt。
- 本地验证：Haven `py_compile` 与全套 unittest 189/189 通过；Dashboard 召回/token/提示词相关测试 31/31 通过，生产 build 通过。Dashboard 全套测试除一条既存的 `display_segments` 版本断言外均通过（测试期待 v1、代码长期输出 v2）；另有一条认证 cookie 用例曾偶发失败，单独重跑通过，两者均与本次改动无关。

## 明确回忆的语义查询超时降级（2026-09-07 本地完成）

- 真实召回透镜已证明部分正确候选不是“语义分低”，而是 `query_timeout` 后完全没有语义分；同一桶在语义成功轮次可正常得到分数并被选中。
- Gateway 的 `embedding.query_timeout_seconds` 默认值从 3 秒调整为 5 秒，降低正常 4–6 秒语义查询被过早切断的概率；召回仍在 Claude 请求前完成，因此最坏情况下首字等待会比原来增加约 2 秒。
- 新增窄降级：仅 `explicit` 且语义状态为 `query_timeout / query_failed / query_embedding_unavailable / query_embedding_failed` 时，候选标题直接命中清理后的可信主题、正式关键词分 `>= 0.65`，才可进入 Utility。原因码为 `shadow_explicit_query_unavailable_title_keyword`。
- `disabled_for_request` 不属于故障；自然 `contextual`、只在正文命中主题、或低于门槛的候选仍不会借此进入，原先“正式候选 + keyword >= 0.85”的保守降级保持不变。
- Dashboard 召回透镜新增标题命中主题、明确回忆标题降级开关和门槛，并为新原因码提供中文解释。
- 本地验证：Haven `py_compile` 通过，召回专项 33/33、全套 unittest 191/191 通过；Dashboard 原因码测试 6/6、生产 build 通过。
- 发布后真实验收已通过：明确生日回忆在 `query_timeout` 下正确选择并最终注入【生日与遗忘的和解】，新规则显示 `shadow_explicit_query_unavailable_title_keyword` 的中文降级说明，Utility 为 `promote`；旧规则仍拒绝，证明本次由重构规则接管。历史 Debug 不会补算新字段。

## rebuilt 候选池补全与取消 freshness 排序（2026-09-07 本地完成）

- 真实“纪念日”案例暴露出候选列表并不完整：新规则此前只审核旧规则最终选中项与旧规则抑制项，旧 admission 已准入但因旧排序落选的候选不会进入 rebuilt，也不会出现在召回透镜。
- rebuilt 现改为审核本轮检索阶段命中的完整候选集合，包含 direct、semantic rescue、relation axis 与 planner supplemental 中已准入但未被旧排序选中的项；不扩大关键词 Top-K 或语义 Top-K，不做全库扫描。
- Planner degraded 的自然 contextual 仍维持 `conservative_no_expansion`，只审核旧正式结果；session 已召回桶和本窗口新 hold 桶的前置/最终硬排除保持不变。
- 候选评分新增 `score_without_freshness`。legacy 继续使用原分数以便回滚；rebuilt 在 relevance/utility 通过后的排序使用无 freshness 分数，长期桶不再因变旧而吃亏。重要度、语义/关键词证据、rerank 与冷却均保留。
- Debug 新增 `reviewed_candidate_count`、`candidate_debug_truncated`，候选记录 `candidate_origin / legacy_score / rebuilt_score / rebuilt_freshness_ignored`。召回透镜合并显示正式抑制和新规则拒绝项，明确标注“旧规则已准入未选”，并区分旧分与新分。
- 本地验证：Haven `py_compile`、召回专项 36/36、全套 unittest 194/194 通过；Dashboard 原因码测试 6/6、生产 build 通过。发布后需用新的“纪念日”轮次确认两个标题直命中桶至少进入完整候选列表；是否最终注入仍由 relevance、utility 和单卡排序决定。

## 发布后仍需继续核查

1. **Embedding 内容新鲜度**：线上 318 个桶已确认没有缺失或模型/维度过期向量，因此不执行 backfill。现有检查不包含正文内容哈希；只有后续出现“桶正文已改但向量未刷新”的具体证据时，再单独审计内容新鲜度。
2. **Semantic 查询状态**：召回透镜已能区分 `scored`、`indexed_not_in_semantic_top_k`、`query_embedding_unavailable/failed`、`query_timeout/failed` 和 engine disabled，并显示明确回忆标题降级证据；发布后需核对真实超时轮次是否命中新原因码。
3. **Session 去重真实契约**：Dashboard 每轮从 Haven 读取 `injected_buckets ∪ session_created_buckets` 并作为 `exclude_ids` 传给 Hook；Hook 先从候选池移除，再在最终出卡前硬过滤，不存在强证据 bypass。因此同一窗口已经返回的桶不重复，当前窗口真正新建的 hold 桶也不参与后续召回或抢占单卡位。`skip_recent_rounds=5` 是另一条旧内部去重路径，不代表 Dashboard 的完整窗口契约。
4. **Planner 实际可用性**：按新记录统计 normal / not_triggered / disabled / degraded，以及 dehydration 鉴权错误；确认用户当前线上是否确有可用 dehydration 配置。Planner 不可用时 contextual 必须保持“不新增”。
5. **Shadow relevance 泛化**：继续收集自然话题、明确过去指向、系统复盘和 keyword-only 噪声案例。重点观察 `semantic >= 0.50 + keyword >= 0.65 + specific topic` 的第一版组合证据是否放过无关桶或漏掉真正相关桶；该阈值只用于 Shadow，线上验收后再决定是否调整。
6. **缺少目标桶的明确请求**：旧 Round 25、54 若对应桶当前不存在，无法证明“明确请求不会整轮误杀”；必须另找已有真实桶的 explicit 案例替代，不把“候选不存在”误判为 gate 失败。
7. **主动召回价值与纯相关性分离**：`natural_contextual_topic` 回答“这句话有没有可检索的具体自然话题”，Shadow relevance 回答“候选是否与话题相关”，新增 utility 三档回答“相关桶当前应优先、保留还是明确拒绝”。自然 contextual 是主要主动召回入口，代码无法确认价值时保持 neutral，不默认沉默。当前代码只覆盖高把握 promote/reject；更细腻的关系连续性仍需先收集真实 neutral 案例，再决定是否增加轻量模型。utility Shadow 稳定前不把 contextual 整体切为正式召回。

## 后续推进顺序

1. Dashboard 召回透镜的单页 Debug 信息、中文映射、部署及 `ob2-20260827-r1bpf2` Round 25 / Round 12 第一组真实验收均已完成。
2. Phase 1 “测试召回 / 观测召回 / 不用搜东西”组合意图优先判 `none` 的修复、回归、完整 SHA 发布和新 session 验收均已完成。
3. 继续用固定案例验收 necessity、候选独立审核、planner 降级不扩召回和 `semantic_status`；embedding 覆盖统计已完成，不做 backfill。
4. Recall utility 与候选 relevance 已按 `promote / neutral / reject` 分离；Dashboard 真实数据已完成三档基础行为和 Shadow 单卡投影验收，Utility MVP 判定通过。
5. 称呼作为明确讨论对象的语境区分另开后续窗口，不与上述 necessity / utility 问题混做。
6. Phase 2 已选择直接接管而非渐进放量：默认最多一张正式桶卡，保留环境变量回滚；不处理家族聚类、自动建边或额外 LLM agent。

Phase 1 necessity/relevance 修正与轻量 utility Shadow 均已 commit/push/deploy，Dashboard 召回透镜展示也已发布；新 session 的 necessity 与 Utility MVP 验收通过。后续仍可用原固定 Round 9、12、25、54、57、61 与 `ob2-20260827-zoazvn` 的 Round 4、6、9、16、20、21、23、24 继续观察候选泛化，但历史 Debug 不会自动补算新字段。在 Shadow 正式接管前，先补一次“那后来呢”上下文指代短测并明确 Phase 2 的渐进开关、回退和验收门槛。

## 下一窗口唯一范围

Phase 2 发布后只做新 session smoke：复测 `none`、自然 `contextual`、明确 `explicit`、完全重复 `reject`，并确认正式结果最多一张、召回透镜能同时看到 legacy/rebuilt/effective ID、同窗口已召回桶和新建 hold 桶不再返回。另用一个标题含明确主题的真实桶重复 explicit 请求，确认语义正常时照常计分；若出现 `query_timeout/query_failed`，应显示新降级并让 keyword `>= 0.65` 的标题直命中候选进入 Utility。`disabled_for_request` 与自然 contextual 不得触发该降级。上下文指代测试若前一句已经返回目标桶，则“那后来呢”因窗口排除而为空是正确结果，不能据此判定 contextual promote 失败；要覆盖 promote，需选一个前句只建立话题但尚未召回目标桶的案例。

## 不得扩散的边界

- `OMBRE_RECALL_DECISION_MODE=rebuilt` 默认正式接管；出现严重回归时只切为 `legacy` 回滚，不现场改阈值。
- Phase 1 不创建家族表、关系边或自动聚类任务。
- 下一窗口只用召回透镜验收 utility 三档和单卡结果；不再要求打开 Gateway Debug，也不扩大其他页面视觉。
- 不用 `localStorage` 作为未来人工标注的唯一存储。
- 旧召回规则暂不删除，作为 `legacy` 回滚路径保留。

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
