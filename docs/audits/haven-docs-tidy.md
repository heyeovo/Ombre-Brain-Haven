# Haven 文档整理核对报告

日期：2026-10-18。范围：OB Todo `9c6c8cedd0ca4860` / dashboard H-02；只改 Haven Markdown，dashboard 只读。状态判定基于当前检出代码，不宣称重新验收生产环境。

## 文档去向

| 文档 | 去向与理由 |
|---|---|
| canonical-domain handoff | `docs/handoff/2026-06-25-canonical-domain-handoff.md`，部分落地：只读规范化视图、Reading Note 已存在，不能据此认定全量迁移完成。 |
| recall-debug handoff | `docs/handoff/2026-08-23-recall-debug-handoff.md`，已作废：旧 gate 修补路线被统一召回取代，历史问题证据保留。 |
| recall-rebuild handoff | `docs/handoff/2026-08-27-recall-rebuild-handoff.md`，已完成归档：Phase 0–2 收口，后续方向由 roadmap 承接。 |
| recall-rebuild plan | `docs/handoff/recall-rebuild-plan.md`，部分落地：统一召回已成立，家族及后续阶段未全部落地，旧双路线开关和两卡方案不再现行。 |
| deploy-zeabur | `docs/archive/deploy-zeabur.md`，已停用，正式部署以 README / AGENTS.md 的 Coolify 流程为准。 |
| INTERNALS | `docs/archive/INTERNALS.md`，旧模块、参数、界面和部署快照不能继续作开发入口。 |
| BEHAVIOR_SPEC | `docs/archive/BEHAVIOR_SPEC.md`，旧启动流程、自动合并、删除、feel、API 降级描述混有过时或互相矛盾内容。 |
| haven-merge-rules | `docs/archive/haven-merge-rules.md`，HEAD/myversion 合并历史与当时待办应保留追溯，现行契约另行提炼。 |

归档正文不逐段重写旧结论；三份旧内部文档均增加归档日期与现行 reference 指引。handoff 状态统一见 `docs/handoff/README.md`。

## 三份旧文档核对与提炼

| 来源 | 已确认并并入 reference | 未并入的内容与理由 |
|---|---|---|
| INTERNALS | Markdown/frontmatter 与独立 embedding SQLite；激活与浮现计时分离；衰减分的短长期权重和状态修正。 | 模块/路由/配置/部署/feel 已由正式文档覆盖；旧自动归档、自动结案、自动合并与 LLM 脱水不成立；硬编码表不作为当前默认值表复制。 |
| BEHAVIOR_SPEC | 创建激活次数为零、resolved 不自动归档；grow 短输入与长摘记失败边界、逐条异常隔离。 | breath/hold/feel/journey/日期/向量召回已有现行契约；旧自动开窗三件套、物理删除、全方法 API 必需、feel 源桶写法及旧搜索准入不照搬。 |
| haven-merge-rules | 普通编辑不刷新激活、journal 独立通道、原文加头格式化、introspection 分页与近期浮现去重、手工 related 与显式边的区别。 | 回收站/评分旋钮/hold 返回/feel 写读已覆盖；旧 Zeabur 部署、前端待办、固定工具返回清单及旧 Gateway 自动扩散正文不作为当前约束。旧 `activate(reset_state=...)` 不符合当前签名。 |

核对入口：`bucket_manager.py` 的 create/update/touch/mark_surfaced/activate/list_all/search，`decay_engine.py` 的 calculate_score/run_decay_cycle，`dehydrator.py` 的 dehydrate，`server.py` 的 grow/introspection/dream，`memory_metadata.py` 的 normalize_memory_metadata，`gateway.py` 的 Reading Note 与统一召回，及现行 reference/recall-pipeline/roadmap。

## 两仓库 reference 去重

Haven 保留 SQLite 表、字段、迁移、事务、CAS、隔离和后端路由；Dashboard 代理路径改指向 dashboard reference「文件结构速查」的 `app/api` 行。协调器/SDK iterator、提示词编辑组装、有效引擎、额度展示、换窗注入和 transcript 迁移规则改指向 dashboard reference「cc 数据持久化契约」。后端必须保存的恢复指针、快照、revision 与排除账本仍在 Haven 说明。

## 不确定与范围限制

- 旧 canonical-domain 方案所说的生产旧桶批量迁移是否实际执行过，不能从只读归一化代码判断；不标为已完成。
- 旧 debug handoff 的 embedding backfill 完成量、模型替换和旧 session 各轮线上效果，本次没有生产数据重验，不将当时记录升级成现行事实。
- 代码注释仍有裸文件名 `BEHAVIOR_SPEC.md`：`config.example.yaml` 两处、`dehydrator.py` 一处。按“只改 .md”保留；它们不是 Markdown 链接。后续如允许代码注释整理，应改指向 `docs/reference.md`「输出格式化与摘记失败边界」，避免继续传播旧的“所有方法无降级”结论。
- 审计报告旧命令输出中的文件路径随归档重定向，但行号仍是当时快照，不代表归档后的行号。

## dashboard 待同步（本次未修改）

1. 验收合并后，将 `TECH_DEBT.md` H-02 移到 `docs/tech-debt-archive.md`，附关闭日期、分支/提交与本报告；删除顶部 H-02 索引行。勾选 OB Todo `9c6c8cedd0ca4860`。
2. `MAINTENANCE_CONTRACT.md` 第四节 Haven 地图：删除旧 Zeabur 指南的现行部署职责，改为 `docs/archive/`（停用 Zeabur 指南和旧 INTERNALS / BEHAVIOR_SPEC / 合并记录，仅历史追溯）；新增 `docs/handoff/README.md`（Haven 历史交接 / 方案状态索引，正文默认不读）。
3. 同节 Haven reference 职责改为“模块 / Haven REST 路由 / 后端持久化与实现契约 / 调试命令”，配置变量清单仍只在 ENV_VARS；Dashboard 代理与客户端运行流程归 dashboard reference。dashboard reference 本次无需配套改写，现有目标节已能承接引用。

本次不用部署；分支推送仅供验收合并。

## 验证

- `.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_reference_routes.py`：2 passed。
- 全仓库检索八个移动文件名，涵盖 README、AGENTS、CLAUDE_PROMPT、docs、tests 与非 Markdown 文件；tests 无旧路径引用。Markdown 中旧显式路径为零，移动文件及 handoff 相对链接 11 处检查均可解析；非 Markdown 仅保留上文列明的三处历史注释。
- `git diff --check` 通过；仅提交 `.md`，既有未跟踪 `.venv` 不纳入提交。
