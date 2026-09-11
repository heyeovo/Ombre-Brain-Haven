# Memory System Roadmap

> 记忆系统后续工作的统一入口。以后开始新的记忆系统任务时先读本文件；只有需要追溯 2026-08 至 2026-09 的召回重构证据时，才阅读历史 handoff。

## 当前阶段

截至 2026-09-09，第一版统一记忆召回已经正式运行并通过线上 smoke。当前策略是持续开启、观察真实体验，不在没有新证据时继续调阈值。

召回已经形成单一路线：

1. 判断本轮是否需要召回：`none / explicit / contextual`。
2. 关键词、语义、精确锚点、关系轴与 planner 补充共同生成有界中性候选池。
3. 统一 relevance 判断候选是否真正相关。
4. Utility 将相关候选分为 `promote / neutral / reject`。
5. 按不含长期 freshness 的分数排序，最终最多注入一张桶卡。

旧 admission gate、旧选卡、semantic rescue 和 legacy 决策切换已退出运行路径。兼容字段 `recall_shadow_debug` 仍保留，但表示统一决策 Debug，不代表另一套 Shadow 算法。

## 已确认的运行边界

- 普通关键词或纯标题命中不能独立证明相关。
- query 语义真实超时/失败时：
  - `explicit` 要求可信主题同时命中标题与正文，keyword `>= 0.65`；
  - `contextual` 要求可信主题命中实质正文，keyword `>= 0.83`。
- `disabled_for_request` 和 `indexed_not_in_semantic_top_k` 不能冒充 query 故障。
- “先看/读/翻找”不会为了使用故障降级而统一改判 `explicit`。
- session 已召回桶和当前窗口新建 hold 桶会被硬排除。
- 家族、关系边、Word Map、reranker 和 planner 未来都只能帮助寻找或组织候选，不能绕过统一 relevance、Utility 与单卡上限。
- Claude/CC MCP 工具提示不是召回算法的调参入口。

详细实现事实见 [recall-pipeline.md](recall-pipeline.md)，记忆分层边界见 [memory-layer-contract.md](memory-layer-contract.md)。

## 现阶段运行方式

保持召回常开，根据真实对话体验决定是否需要调整。一次偶发感觉不直接改规则；先在召回透镜中保存能复现的证据。

需要重新进入召回调整的三类信号：

1. **稳定漏召**：目标桶存在且进入检索范围，但多次没有进入 Utility 或最终结果。
2. **稳定误召**：与当前话题无关的桶多次进入 Utility 或注入。
3. **可靠性/延迟问题**：query timeout、embedding failure 或 planner degraded 频率明显影响体验。

每个案例至少记录：

- 原始用户句子与必要性判断；
- query 语义状态；
- 实际审核候选与最终注入 ID；
- semantic、keyword、正文主题命中；
- relevance/Utility 原因；
- session 是否已有排除项；
- 预期应该召回或不召回的具体桶。

没有这些证据时，不调整阈值、不增加旁路。

## 后续规划

### Phase 3：记忆家族聚类 Shadow

目标是把多个相关桶组织成可观察的“家族候选”，解决同一主题分散在多条记忆中、直接检索难以理解整体的问题。

本阶段只生成和展示候选家族：

- 不改变正式召回；
- 不自动修改、合并或删除桶；
- 不自动创建关系边；
- 每个家族必须能说明成员为什么聚在一起；
- 支持人工查看错误成员、漏掉成员和主题命名质量；
- profile/persona 必须严格隔离。

开始实现前需要单独确认：

- 家族是持久化实体还是可重建派生索引；
- 家族候选使用哪些证据；
- 人工纠错保存在哪里；
- 如何衡量家族纯度、覆盖率和稳定性；
- Dashboard 最小观察面需要展示什么。

验收重点不是“聚出很多家族”，而是典型主题成员聚合正确、无关桶不会因为泛情绪或身份背景被混入。

### Phase 4：家族辅助候选召回

仅在 Phase 3 人工验收稳定后开始。家族与直接桶检索并行：

- 直接桶命中仍可独立工作；
- 家族只能补充有界候选，不能成为唯一入口；
- 家族展开后的每个桶仍逐个经过统一 relevance 与 Utility；
- 继续遵守 session 排除和最多一张正式桶卡。

### Phase 5：明确关系边与一跳扩展

为“同一事件、后续、因果、状态更新、冲突”等明确关系建立可解释边。默认只允许一跳、有上限的扩展，不进行全库两两建边或无界多跳。

关系边首先 Shadow 展示和人工验收；稳定后也只能帮助候选发现，不能直接决定注入。

### Phase 6：评估额外 LLM 判断

只有在统一规则、家族和关系边仍留下稳定且高价值的判断缺口时，才评估轻量模型。模型不得替代硬排除、编造关系或无证据扩大候选；必须先定义成本、超时降级与可观测性。

## 已知但不立即处理的观察项

- 配置称呼作为明确讨论对象时，如何从“默认中性称呼”恢复为主题证据。
- embedding 是否需要正文内容哈希来检查内容新鲜度。
- query timeout/failed 的长期频率与延迟分布。
- Utility 对更细腻关系连续性的判断是否需要增强。
- 家族聚类的存储、人工纠错和重建契约。

这些都是正式规划的一部分，但当前没有实施排期。出现真实证据或准备启动对应 Phase 时，再为该阶段建立窄范围 handoff。

## 下一次从哪里开始

下次要调整记忆系统时：

1. 先读本文件；
2. 涉及当前召回实现时，再读 [recall-pipeline.md](recall-pipeline.md)；
3. 涉及记忆层归属或持久化时，再读 [memory-layer-contract.md](memory-layer-contract.md)；
4. 从一个明确问题或一个 Phase 的最小范围开始，不重新通读历史长 handoff。

历史资料：

- [2026-08-27 recall rebuild handoff](2026-08-27-recall-rebuild-handoff.md)
- [recall rebuild 总体方案](recall-rebuild-plan.md)

