---
name: haven-merge-rules
description: Ombre-Brain-Haven merge rules and key design decisions
metadata:
  type: project
---

# Ombre-Brain-Haven Merge 合并记录

## 合并原则

- HEAD（Haven/二改版）逻辑为主
- myversion（Ombre-Brain 自用版）独有功能合并进来，主要是 journal/trash 相关
- 二改版改动过的原有逻辑不被自用版覆盖
- 两边都有且互补的字段/参数 → 全部保留

## 已解决文件

| 文件 | 策略 |
|------|------|
| `.gitignore` | 两边都保留（互补） |
| `INTERNALS.md` | 合并：HEAD 7 Tab + 自用版认证文档段 + 设置 Tab |
| `README.md` | 全部 HEAD（全新的 Haven 文档） |
| `dashboard.html` | 全部 HEAD |
| `dehydrator.py` | 全部 HEAD |
| `embedding_engine.py` | 全部 HEAD（env var 覆盖由 utils.py 统一处理） |
| `import_memory.py` | 全部 HEAD |
| `config.example.yaml` | 全部 HEAD |

## 关键设计决策

### 1. anchor / pinned / self_anchor 三者共存

| | pinned/protected | anchor | self_anchor |
|---|---|---|---|
| 设定者 | 用户手动 | 系统/用户标记 | 系统初始设定 |
| 衰减 | ×999 永不衰减 | 正常衰减 | 正常衰减（不参与普通召回竞争） |
| 浮现 | core_candidates | handoff + 可选锚点浮现 | 仅 handoff |
| 可修改 | 是 | 是 | 只读 |

### 2. 时间刷新策略 → 选 myversion

编辑操作（update）不自动刷新 `last_active`/`activation_count`。
理由：人为编辑元数据是维护行为，不应影响浮现权重。只有真正的 touch（检索命中）才刷新。

### 3. 删除机制 → 选 myversion 的 trash

- trash 目录 + restore/purge/empty_trash/list_trash
- 墓碑（tombstone）不保留
- 理由：trash 可恢复 + ob-dashboard2 前端已对接

### 4. 搜索评分引擎 → HEAD 的 BM25 + myversion 的旋钮

- 主体：HEAD 的 `calc_topic_scores()` + BM25 + IDF/文档长度归一化 + CJK 短查询 + lexical profile 缓存
- 可视化：`_calc_topic_match()` 输出 `matched_in`/`field_scores`（HEAD server.py 已在用）
- 运行时旋钮：合并 myversion 的 title_hit_bonus/keyword_first_sort/precise_match_mode/keyword_bypass/token_exact_match/warmth_boost + 热更新 API
- jieba 分词：两边都有，保留 HEAD 的 `_lexical_tokens()`（内部已用 jieba）

### 5. search() 参数默认值

- `include_archive: False` — 默认不浮现归档桶
- `show_all: False` — 只影响 Dashboard 前端，不影响 LLM MCP 调用
- `include_noise: False` — 默认排除噪声
- `record_stats: True`

### 6. activate() → 合并版

HEAD 的 activate 逻辑 + myversion 的 feel 类型恢复，加 `reset_state` 参数。

### 7. bucket_manager.py create/update 字段全量合并

HEAD 新增字段：bucket_id/source/anchor/resolved/digested/confidence/period/date/extra_metadata/comments/profile_kind/evidence/source_bucket_ids 等
myversion 新增字段：wish/todo/author/locked/unlock_hint/event_time/related
→ 全部保留，互补不冲突

### 8. myversion 的 journal 体系 → 全部保留

- `journal_dir` / `list_journal()` / `convert_to_journal()` / `delete_journal()`
- journal 是完全独立通道，不进 list_all()/search()
- ob-dashboard2 有对应的 journal API

### 9. dehydrate() 返回原文，不做 LLM 压缩

HEAD 的 `dehydrator.dehydrate()` 已改为直接返回原文 + metadata header（📌 记忆桶名 [主题] [情感]），**不再走 LLM 做 JSON 压缩摘要**。
旧版那条链路在短桶上反而让 token 翻倍（200字桶压缩后 JSON 反而 400字），且压扁了原文情绪温度。
`_format_output()` 只做：包一层元数据头 + 原文。MCP breath 返回的就是原文 + 头。

### 10. hold() 的三种"感受写入"路径（HEAD）

```
hold(content="...", feel=True)
  ├─ 有 source_bucket → add_comment (年轮, kind="feel") 挂在该桶下
  └─ 无 source_bucket → create_whisper_bucket() 独立桶

hold(content="...", whisper=True)
  └─ create_whisper_bucket() 独立桶（tag 带 "whisper"）
```

| | comment_bucket | feel=True | whisper=True |
|---|---|---|---|
| 本质 | 独立 MCP 工具 | hold 快捷方式 | hold 快捷方式 |
| 有源记忆 | ✅ 挂在源桶 | ✅ → 转调 add_comment | ❌ 报错 |
| 无源记忆 | N/A | ✅ → create_whisper_bucket | ✅ → create_whisper_bucket |
| 存储形式 | comment 在源桶 | 年轮 or 独立 feel 桶 | 独立 feel 桶 (tag:whisper) |
| 读取方式 | 读源桶时在 comments[] | breath(domain="feel") | breath(domain="whisper") |

**whisper ≈ 以前 myversion 的 feel（无源独立感受）**。
HEAD 把无源/有源拆成两个入口，底层同一套逻辑，只是 API 语义更清晰。

## 环境变量策略（Haven）

不在 Zeabur 逐个设 env var。策略：
- 只设 4 个密钥 env var：OMBRE_API_KEY / OMBRE_EMBEDDING_API_KEY / OMBRE_GATEWAY_TOKEN / OMBRE_DASHBOARD_PASSWORD
- 其余配置全在 config.yaml，挂载持久化 Volume
- 运行时通过 Dashboard 热更新，回写 config.yaml（或 fallback state/config.runtime.yaml）
- Provider 密钥通过 gateway.upstreams[*].api_key_env 指向对应 env var

---

## breath() 完整架构

### MCP 调用版

`breath()` 是 LLM 主动调用的 MCP 工具入口。

**入口路由**（按参数走不同分支）：
```
breath(...)
  ├─ mode="handoff"           → 新窗口轻交接（自我/画像/近期连续性）
  ├─ domain="feel/whisper/daily_impression" → 独立只读通道
  ├─ date="xxx"               → 按日期检索记忆
  ├─ importance_min>=1        → 重要度批量拉取
  ├─ domain="journey"         → 轨迹桶独立通道
  ├─ domain="journal"         → 日记通道（含上锁检测）【MV】
  ├─ 无 query                  → 浮现模式（权重池主动推送）
  └─ 有 query                  → 搜索模式（BM25 + 向量双通道）
```

**浮现模式**（无 query）：
```
Step 1: 桶分三类
  ├─ Core: protected（全部）+ pinned（限 core_limit=3）
  ├─ Anchor: 最多 2 条（anchor=True 且非 pinned）
  └─ Unresolved: 排除 self_anchor/permanent/feel/anchor/pinned/protected
               按 decay_engine.calculate_score() 排序

Step 2: 冷启动检测（activation_count==0 && importance>=8 → 最多 2 条插最前）

Step 3: 多样性采样（Top-1 固定 + 第2~20条随机打乱）

Step 4: 分层 Token 预算（Core 25% → Anchor 18% → Dynamic 剩余）
        每条走 dehydrate() 返回原文+元数据头

Step 5: 追加块
  ├─ related_block: 图扩散联想记忆【HEAD】
  ├─ dream_block: dream_engine 潜伏梦境【HEAD】
  └─ wish_result: 15%概率随机浮现一条 wish 记忆【HEAD + MV 共有】

Step 6: 组装返回 "=== 核心准则 === / === 长期锚点 === / === 浮现记忆 === / ..."

注意：浮现不做 touch() — 浮现不是检索命中，不影响衰减权重
但会做 mark_surfaced()（出自MV）— dream 去重用
```

**搜索模式**（有 query）：
```
Step 1: recall_search_query() 预处理 query【HEAD】
Step 2: bucket_mgr.search() → BM25 评分 → 向量扩展
Step 3: 排除 journey domain（除非显式 domain="journey"）
Step 4: admission gate 可靠性检查
Step 5: dehydrate 返回原文+元数据头
```

### Gateway 注入版

Gateway 不调 `breath()` MCP 工具，走自己的**后台召回管线**：
```
每条用户消息
  ├─ Just Now Context         最近几轮对话原文
  ├─ Operit Context           设备/工作区/照顾备忘拆包
  ├─ Favorite Memory          印象深刻的记忆（间隔轮次）
  ├─ Date Recall              明确日期的原文检索
  ├─ Recalled Memory          动态记忆召回（moments + 图扩散）
  ├─ Related Memory           图扩散关联记忆
  ├─ Targeted Memory Detail   根据 bucket_id 精确读取全文
  ├─ Recent Context           近期事件摘要（冷却机制）
  └─ Dream Context            后台潜伏梦
```
→ 全部组装进 system message 的 additional_context 块

| | MCP breath() | Gateway 注入 |
|---|---|---|
| 触发 | LLM 主动调工具 | 每条用户消息自动跑 |
| 返回格式 | 纯文本（给 LLM 看） | 注入 system message |
| 浮现模式 | ✅ 权重池推送 | ❌ 无 |
| 图扩散 | 有限（related_block） | ✅ 完整 pipeline |
| 门控 | 有 admission gate | 有，更严格 |
| 冷启动 | ✅ | ❌ |

### breath 功能归属

| 功能 | 出自 |
|---|---|
| handoff 模式 | HEAD |
| core/anchor/dynamic 分层浮现 | HEAD |
| 冷启动检测、多样性打乱 | HEAD |
| dream_block/related_block | HEAD |
| mark_surfaced / last_breath_surfaced 去重 | **MV** |
| journey/journal domain 独立通道 | 两边共有 |
| importance_min 批量拉取 | 两边共有 |
| search_query 预处理 + 门控 | HEAD |
| wish 随机浮现 | **MV**（HEAD 也内置了相同逻辑） |

---

## dream/introspection 结构

`dream()` 已改名 `introspection()`（`dream()` 保留为兼容旧名，内部转发）。

**流程**：
```
Step 1: list_all() 筛选普通记忆（排除 permanent/feel/journey/pinned/protected）
Step 2: 排除 10min 内被 breath mark_surfaced 过的桶【MV 的去重】
Step 3: 按 created 排序 + offset/limit 分页【HEAD】
Step 4: 每条返回：名字 + 状态 + 主题域 + 情感坐标 + 原文前 500 字

Step 5: 追加 hints
  ├─ connection_hint: embedding 找两两最相似记忆对，提示关联【HEAD】
  ├─ crystal_hint: ≥3条 feel 相似度>0.7 → 提示升级 pinned【HEAD】
  └─ profile_fact_hint: 正则匹配"喜欢/讨厌/偏好/雷点" → 提示写 profile_fact【HEAD】

Step 6: 返回纯文本，引导 LLM："值得放下的用 trace(resolved=1)，有沉淀的用 comment_bucket(...)"
```

---

## 相关调试 API 清单

### MV 的 API（ob-dashboard2 正在用，必须保留）

| API | 用途 |
|---|---|
| `/api/breath-debug?q=...` | 模拟搜索，返回每条桶四维评分拆解 |
| `/api/search?simulate=true` | 即时模拟搜索 |
| `/api/hit-stats` | 每个桶的被检索命中统计 |
| `/api/hit-stats/reset` | 清零命中统计 |
| `/api/recent-searches` | 最近搜索追溯 |
| `/api/scoring-config` GET/POST | 评分配置热调整 + 持久化到 runtime_config.json |
| `/api/scoring-config/reset` | 重置评分配置 |

### HEAD 的 API

| API | 用途 | 前端 |
|---|---|---|
| `/api/diffusion-debug?q=...` | 图扩散诊断（seeds→edges→hits→paths） | HEAD dashboard.html |
| `/api/recall-debug?q=...` | query→moment 召回候选诊断 | HEAD dashboard.html |

### 认证 API（统一用 HEAD 的 `_dashboard_sessions` 字典）

`POST /auth/login` 登录写入 `_dashboard_sessions`，cookie 名 `ombre_session`。
ob-dashboard2 前端**不需要改动**。

---

## ob-dashboard2 可新增的前端展示

这些是后端已有但前端还没有页面的功能：

| 功能 | 说明 | 优先级 |
|---|---|---|
| **扩散诊断页** | 调用 `/api/diffusion-debug` 展示图扩散链路，调搜索参数时参考 | 中 |
| **召回诊断页** | 调用 `/api/recall-debug` 看候选 moment 怎么来的 | 低 |
| **Journal 管理页** | 日记桶列表/查看/解锁，调用 `/api/journal` 系列 | 低（已有纯 API） |
| **Reminder 管理页** | 照顾备忘列表/增删，调用 `/api/reminders` | 低 |
| **hit_stats 可视化** | 把命中统计做成图表/排行榜 | 低（已有基础页面） |

---

## related 的两个概念

| | HEAD related_block | MV related 字段 |
|---|---|---|
| 层 | server.py breath 浮现/搜索时自动图扩散 | bucket_manager.py 元数据字段 |
| 机制 | 沿 memory_edges 一跳扩散，用 diffuse_memory() 找关联 | trace 时手动传 `related=id1,id2` |
| 触发 | 浮现模式 + 搜索模式都触发 | 人为指定 |
| 存储 | state/memory_edges.jsonl | frontmatter related 字段 |

**互补不冲突，共存。**

---

## 认证系统统一

HEAD 叫 `_require_dashboard_auth` + `_dashboard_sessions`，MV 叫 `_require_auth` + `_sessions`。
底层逻辑完全一样（cookie `ombre_session` + SHA256 密码）。
**统一为 `_require_dashboard_auth` + `_dashboard_sessions`**，ob-dashboard2 前端不需要改。

---

## MCP 工具返回格式精简（2026-07-18）

全部从 JSON dict 改为纯文本，节省 token：

| 工具 | 之前 | 现在 |
|---|---|---|
| **read_bucket** | `dict` — 30+ 字段 | `str` — `[标题][bucket_id] [重要性:5] [日期:2026-07-08]\n\n正文\n\n[年轮#1]...` |
| **comment_bucket** | `dict` — 含完整 metadata | `str` — `年轮→{id}#{comment_id}` |
| **delete_bucket_comment** | `dict` — 含完整 metadata | `str` — `已删除年轮 {id}#{comment_id}` |
| **reminder_create** | `dict` — 含完整 reminder 对象 | `str` — `已创建照顾备忘 [{id}] {title}` |
| **reminder_list** | `dict` — 含完整对象 | `str` — 总数 + 每条 id/title/content/start_at/end_at/status |
| **reminder_update** | `dict` — 含完整对象 | `str` — `已更新照顾备忘 [{id}] {title} → {status}` |
| **darkroom_enter** | `dict` | `str` — `暗房 → [{room_id}] #{rev} ({entry_id}) [锁至...]` |
| **darkroom_rooms** | `dict` | `str` — 房间列表含锁状态 |
| **darkroom_view** | `dict` | `str` — 正文 + 修订历史 |
| **darkroom_release** | 无 MCP 工具 | `str` — `暗房显影 [{room_id}] ({entry_id})\n\n{content}` |

## darkroom lock_for 扩展

正则支持分钟级：`5m` / `5min` / `5分钟` / `6h` / `3d` 等。见 `darkroom.py:_parse_lock_for()`。

## Reminder 两个通道

| 通道 | 触发方式 | 说明 |
|---|---|---|
| **MCP 手动** | LLM 调用 `reminder_create/list/update` | 创建/查看/标记完成 |
| **Gateway 自动注入** | 每条用户消息时检查 `reminder_store.due()` | 到期 reminder 注入到 context 里 |

配置：`gateway.active_reminders_enabled` / `active_reminder_inject_limit`。

---

## 当前部署状态

| 环境 | 状态 |
|---|---|
| **本地 Docker** | Brain + Gateway 均可跑（`compose.local.yml`） |
| **Zeabur（方案 A）** | Brain-only 已部署，Health 正常，279 桶已迁移，Dashboard 可用 |
| **Zeabur Gateway** | 待部署（需同一容器双进程 or 拆双服务共享 Volume） |
| **GitHub** | `heyeovo/Ombre-Brain-Haven`，所有改动已 push |

## 待办（新窗口处理）

| 优先级 | 事项 | 说明 |
|---|---|---|
| **P0** | Gateway 注入完整测试 | 召回、门控、注入格式端到端 |
| **P0** | ob-dashboard2 添加 `X-Ombre-Session-Id` | 每个窗口生成独立 session_id，触发 handoff |
| **P0** | ob-dashboard2 prompt 拼接 | 工具描述 + Gateway context + 自定义系统指令的统一组装 |
| **P1** | Zeabur Gateway 部署 | 同容器双进程 or 拆双服务共享 Volume |
| **P1** | 召回准确度调参 | 门控阈值、关键词/语义融合权重 |
| **P2** | 前端 Session 管理 | 换窗缓存清理、handoff 轮次标记 |
| **P2** | ob-dashboard2 暗房管理页 | `GET /api/darkroom/rooms` + `darkroom_view` |
| **P2** | ob-dashboard2 Reminder 管理页 | 已有 `/api/reminders`，加前端页面 |
