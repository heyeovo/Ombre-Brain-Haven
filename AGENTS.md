# Ombre-Brain-Haven 项目规则

本文件只记录 Haven 特有的代理工作约束。通用的调查纪律、修改确认、git 和换窗归档规则继承用户全局 `AGENTS.md`。

## 文档职责

- `CLAUDE.md` 记录当前已经成立的模块、路由和实现契约，不记录阶段进度或后续窗口任务。
- 系统级总览、部署和客户端接入以 `README.md` 为准；环境变量以 `ENV_VARS.md` 为准。
- 跨仓库改动完成后，按相邻 dashboard 仓库的 `MAINTENANCE_CONTRACT.md` 判断需要同步的文档。
- 已排入后续窗口的工作写入对应 handoff；没有明确排期的长期遗留才写入 dashboard 仓库的 `TECH_DEBT.md`。

## 持久化与迁移

- 需要跨重启、跨部署或跨设备保留的 cc 配置和用户数据，以 Haven 持久层为事实源，不能把进程内存、临时目录或浏览器状态作为唯一存储。
- 数据库表结构变更必须兼容已有数据库，初始化迁移必须可重复执行；同时补旧库升级和重复初始化测试。
- 会话数据的新增、读取和删除必须保留 `profile_id` 隔离。旧表做不到安全隔离时，宁可暂不删除并记录技术债务，不得扩大删除范围。
- 含密钥配置不能返回浏览器；浏览器只接收掩码后的值。

## 架构文档

- 动态召回 pipeline 架构见 `docs/recall-pipeline.md`，排查召回问题时先读这个文件再进代码。

## 验证与 Coolify 发布

- 涉及 VPS、Coolify、发布、回滚或 Dashboard/Haven 跨仓库联动时，开始前必须同时读取本文件与相邻 `ob-dashboard2/AGENTS.md`；不能只读当前仓库规则。
- 修改 Haven 代码后运行与改动对应的测试；涉及持久化契约时至少覆盖迁移、幂等、冲突和隔离边界。
- VPS 正式 Haven 是 Coolify 中保存 Compose 的手动 Service，不绑定 Git push；用户 commit + push 只更新 GitHub，不会上线，也不再以 Zeabur deployment 作为验收目标。
- Brain 与 Gateway 的构建源共同读取必填的 `HAVEN_RELEASE_SHA`。需要正式发布时，必须先提醒用户复制已验收 commit 的完整 SHA，在 Coolify `Ombre Brain → production → haven-test-stack → Environment Variables` 更新该值，再执行普通 Restart/Deploy；不得选择 `Restart (pull latest)`。
- 发布后必须查看构建/部署输出，确认目标 SHA 被采用，并等 Brain 与 Gateway 都恢复 `Running (healthy)`；只看到 GitHub push 成功不算部署完成。
- 回滚时把 `HAVEN_RELEASE_SHA` 改回上一完整 SHA 后重新部署。旧代码可能与当前正式数据不兼容，实际回滚前必须再次取得用户确认，不得为了验证路径擅自让正式数据运行旧代码。
- 每次涉及可部署代码的任务收尾，都要主动告诉用户“本次是否需要上线”。需要上线时给出上述点击路径；不需要上线时明确说“本次不用部署”。不得默认 commit/push 已经更新 VPS。
