# Ombre-Brain-Haven 项目规则

Ombre Brain 记忆系统后端（Python FastMCP + Starlette，Brain `server.py` + Gateway `gateway.py` 双进程），和前端 ob-dashboard2 一起部署在 VPS，由 Coolify 管理。

本文件是本仓库唯一的入口：Codex 自动读取，Claude Code 通过 `CLAUDE.md` 的 `@AGENTS.md` 读取。只放每次都要知道的东西，查表资料放 `docs/reference.md`。通用协作规范（先讨论后动手、不扩散范围、git）以 dashboard `AGENTS.md`「协作规范」为准；Codex 另有用户全局 `AGENTS.md` 时一并遵守。

## 开工必读

| 什么时候 | 读什么 |
|---|---|
| 每个工作窗口 | 本文件 + dashboard 仓库 `MAINTENANCE_CONTRACT.md` |
| 涉及 Dashboard、VPS、Coolify、发布、回滚 | dashboard 仓库 `AGENTS.md` |
| 动召回 / 排查召回问题 | `docs/recall-pipeline.md`，再进代码 |
| 动记忆系统整体方向 | `docs/memory-system-roadmap.md` |
| 找模块、REST 路由、实现细节、调试命令 | `docs/reference.md`，先 Grep 再定点读 |

## 文档职责

- `AGENTS.md`：入口、必读清单、硬规矩。
- `docs/reference.md`：当前已经成立的模块、路由和实现契约，不记录阶段进度或后续窗口任务。
- 系统级总览、部署和客户端接入以 `README.md` 为准；环境变量以 `ENV_VARS.md` 为准。
- 给外部模型的行为指引：`CLAUDE_PROMPT.md`、`docs/Tool Guide.md`。
- 跨仓库改动完成后，按相邻 dashboard 仓库的 `MAINTENANCE_CONTRACT.md` 判断需要同步的文档。
- 待办分流见 dashboard 仓库 `MAINTENANCE_CONTRACT.md` 铁律 4：想做的活 → OB Todo，代码债和技术卡 → dashboard `TECH_DEBT.md`，handoff 是历史档案（状态看 dashboard `docs/handoff/README.md`）。

## 持久化与迁移

- 需要跨重启、跨部署或跨设备保留的 cc 配置和用户数据，以 Haven 持久层为事实源，不能把进程内存、临时目录或浏览器状态作为唯一存储。
- 数据库表结构变更必须兼容已有数据库，初始化迁移必须可重复执行；同时补旧库升级和重复初始化测试。
- 会话数据的新增、读取和删除必须保留 `profile_id` 隔离。旧表做不到安全隔离时，宁可暂不删除并记录技术债务，不得扩大删除范围。
- 含密钥配置不能返回浏览器；浏览器只接收掩码后的值。

## 验证与 Coolify 发布

- 涉及 VPS、Coolify、发布、回滚或 Dashboard/Haven 跨仓库联动时，开始前必须同时读取本文件与相邻 `ob-dashboard2/AGENTS.md`；不能只读当前仓库规则。
- 修改 Haven 代码后运行与改动对应的测试；涉及持久化契约时至少覆盖迁移、幂等、冲突和隔离边界。
- VPS 正式 Haven 是 Coolify 中保存 Compose 的 Service，不直接绑定 Git source；`main` 的 push 先运行 GitHub Actions `Tests`，测试成功后由 `deploy-haven` job 通过 Coolify API 把 `HAVEN_RELEASE_SHA` 更新为该次完整 commit SHA，再触发普通部署。测试失败或 pull request 不得触发正式部署，也不再以 Zeabur deployment 作为验收目标。
- Brain 与 Gateway 的构建源共同读取必填的 `HAVEN_RELEASE_SHA`。自动发布必须继续固定完整 SHA，不得改为跟随 `main` 或 `latest`，也不得选择 `Restart (pull latest)`。自动化不可用时，才由用户在 Coolify `Ombre Brain → production → haven-test-stack → Environment Variables` 手动更新已验收的完整 SHA 后执行普通 Restart/Deploy。
- 发布后必须查看构建/部署输出，确认目标 SHA 被采用，并等 Brain 与 Gateway 都恢复 `Running (healthy)`；只看到 GitHub push 成功不算部署完成。
- 回滚时把 `HAVEN_RELEASE_SHA` 改回上一完整 SHA 后重新部署。旧代码可能与当前正式数据不兼容，实际回滚前必须再次取得用户确认，不得为了验证路径擅自让正式数据运行旧代码。
- 每次涉及可部署代码的任务收尾，都要主动告诉用户“本次是否需要上线”。需要上线时给出上述点击路径；不需要上线时明确说“本次不用部署”。不得默认 commit/push 已经更新 VPS。
