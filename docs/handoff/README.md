# handoff 状态索引

> 2026-10-18 整理。这里的文件都是历史档案：记录当时的方案、验收和踩坑，默认不读，需要追溯时再定点查。
> 正文中的“待实施 / 待部署 / 下一步”是写作当时的状态，以本索引为准；待办看 OB Todo，代码债与技术卡看 dashboard `TECH_DEBT.md`。
> 当前实现看 [reference](../reference.md) 与 [召回链路](../recall-pipeline.md)，后续方向看 [roadmap](../memory-system-roadmap.md)；正式部署见根目录 README / AGENTS.md。

| 文件 | 状态 | 备注 |
|---|---|---|
| [2026-06-25-canonical-domain-handoff.md](2026-06-25-canonical-domain-handoff.md) | 🧩 部分落地 | `memory_metadata.normalize_memory_metadata()` 已提供只读分类视图，Gateway 已有 Reading Note；不能把这些视为方案中全量字段写入与旧桶批量迁移已完成。 |
| [2026-08-23-recall-debug-handoff.md](2026-08-23-recall-debug-handoff.md) | 📦 已作废 | 旧 admission gate 的补丁与验收基线已由 `recall_policy.py`、Gateway 统一 relevance/Utility 路线取代，保留作问题来源证据。 |
| [2026-08-27-recall-rebuild-handoff.md](2026-08-27-recall-rebuild-handoff.md) | 📦 已完成归档 | Phase 0–2 已收口，当前代码和召回链路均为统一候选、relevance、Utility、单卡路线；家族等后续方向已转入 roadmap。 |
| [recall-rebuild-plan.md](recall-rebuild-plan.md) | 🧩 部分落地 | 观测与统一召回已落地，Phase 3–6 家族及后续扩展仍属 roadmap；原方案的双路线回退开关与最多两卡已不适用。 |
