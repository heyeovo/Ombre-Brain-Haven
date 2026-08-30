# Ombre-Brain 开发文档

> 供新窗口快速了解后端全貌，开窗口时 fetch 此文件。
> 系统级总览 / 部署 / 客户端接入以 **README.md** 为准（Haven/Rain Fork 架构）。本文件只做开发入口：模块、路由、实现细节。
> Codex/代理在本仓库中的工作约束见 **`AGENTS.md`**；改动收尾契约见 dashboard 仓库 **`MAINTENANCE_CONTRACT.md`**。
> 已排期的后续工作写入对应 handoff；没有近期排期的长期遗留见 dashboard 仓库 **`TECH_DEBT.md`**。本文件不维护阶段任务副本。

## 项目概述

Ombre Brain 是 AI 长期情绪记忆系统后端。当前为 Haven/Rain Fork：Python FastMCP + Starlette，**Brain（`server.py`）与 Gateway（`gateway.py`）双进程**，部署在 Zeabur。前端 ob-dashboard2（Next.js 15）部署在 Vercel。

- **仓库**：github.com/heyeovo/Ombre-Brain-Haven
- **Zeabur 域名**：https://foryan.zeabur.app
- **前端仓库**：github.com/heyeovo/ob-dashboard2

## 技术栈

- **语言**：Python 3.10+
- **框架**：FastMCP + Starlette（HTTP 模式）
- **关键依赖**：jieba（中文分词）、httpx（LLM API 调用）、PyYAML、mcp（钉死 1.27.2，防 Zeabur ModuleNotFoundError）
- **LLM**：通过 OpenAI 兼容 API 调用（`OMBRE_API_KEY` + `OMBRE_BASE_URL` 配置）

## 启动方式

```bash
# 安装依赖
pip install -r requirements.txt

# stdio 模式（本地 Claude Desktop）
python server.py

# HTTP 模式（远程部署 / 前端 dashboard）
OMBRE_TRANSPORT=streamable-http python server.py
```

## 核心模块

| 文件 | 职责 |
|------|------|
| `server.py` | **Brain** 入口（~640KB）。MCP 工具注册（`@mcp.custom_route`）+ REST API + 记忆核心 |
| `gateway.py` | **Gateway** 入口（~965KB）。OpenAI 兼容转发 + `/gateway` 前缀路由 + 注入/召回管线 + cc 持久化路由（`Route()` 注册） |
| `gateway_state.py` | Gateway/cc SQLite 状态：会话原文、窗口闲聊/工作模式、固定日回顾、handoff 与 CC 缓存前缀快照、全局 Pro 额度快照、独立 `daily_reviews`、图片/文件附件、协作者归属与提示词、幂等写入、跨设备冲突、CC Pro/API 分线路 session 与游标、Context GC 配置/历史、桶排除账本 |
| `prompt_store.py` | 四类产品 Prompt 覆盖持久化：按 profile 保存 `analyze`、`merge`、`daily_review`、`weekly_journey` 用户版本、revision 与更新时间；代码默认仍是系统真源 |
| `automation_store.py` | 通用自动化 SQLite 控制面：持久 schedule、逐任务 API/Pro 选择、实际 execution、run、candidate，兼容旧库重复迁移；候选 revision CAS、批准冻结、执行状态和任务 lease 与普通记忆桶隔离 |
| `automation_model_runner.py` | 仅为 `daily_review` / `weekly_journey` 按 Haven 持久选择调用既有 API client 或 Dashboard Claude Pro runner；Pro 入口缺失、额度/登录/网络失败均原样失败，不自动 fallback |
| `automation_scheduler.py` | 日回顾与 weekly journey 的香港时区分钟级调度规则：固定 04:00 日界线、下次运行计算、连续轨迹截止游标和可编辑时间校验 |
| `automation_executor.py` | 人工批准候选的白名单执行器；当前只注册 `weekly_journey`，负责冲突校验、批准稿 hash、派生 operation ID、重复确认回放、两步切换恢复及确认完成时原子推进轨迹游标 |
| `journey_weekly_engine.py` | 每周触发的 journey 只读输入聚合与严格三类候选生成；从已确认截止日下一天连续读取到最近完整 OB 日，积压单次最多 31 天，读取开放阶段、日回顾、新桶/独立 feel/旧桶 feel 年轮并排除旧日印象/关系天气 feel |
| `bucket_manager.py` | 桶 CRUD、搜索、评分、回收站、命中统计、分词 |
| `dehydrator.py` | LLM 脱水、合并、打标；三类调用共用可热更新的 dehydration token、temperature、thinking 参数（含 `_last_merge_usage` 成本追踪） |
| `decay_engine.py` | 衰减引擎，只计算排序 score；不自动 resolved、digested 或 archive |
| `embedding_engine.py` | 向量嵌入 + 相似度搜索；可区分当前模型可用、缺失、损坏及模型/维度过期的桶向量状态 |
| `import_memory.py` | 对话历史导入引擎（含成本追踪） |
| `raw_events.py` | 隔离的原文 SQLite、显式原文检索、运行时/历史档案 scope、历史窗口目录与按时间分页读取、消息与导入幂等、私密白名单聊天档案分块归档 |
| `raw_archive_import.py` | Claude 官方与 Kelivo 导出的流式白名单适配、预览审计、跨来源疑似重复、可见聊天＋推理档案打包及确认式上传 CLI；工具和附件内容不入 Haven |
| `repair_raw_archive_thinking.py` | 历史档案正文/thinking 一次性幂等修复；默认 dry-run，只更新正文或 thinking 确有差异的消息；`--apply` 使用整批事务从 Haven 私密归档回填并重建正文哈希/FTS，异常时整批回滚 |
| `recall_policy.py` | 召回策略（none/explicit/contextual 必要性、vague 闸、相对日期、分词整词判断） |
| `reflection_engine.py` | 反思/日印象引擎 |
| `daily_review_engine.py` | 默认每日 04:30 按协作者生成第一人称日回顾；D 日材料固定为 D 日 04:00–D+1 日 04:00，安全字符预算内直接使用全部可见正文，超预算才按工作窗口压缩较早轮次并保留每窗最近原文；连续性参考仍为 D-2、D-1 两条日回顾，可由逐任务 router 选择 API 或 Pro，结果不进记忆桶 |
| `persona_engine.py` / `portrait_engine.py` | 用户画像（persona 状态 + 画像生成） |
| `memory_*.py` | 记忆分层：layers/nodes/edges/metadata/moments/diffusion/relevance/write_gate |
| `todo_store.py` / `reminder_store.py` | 待办 / 照顾备忘持久化 |
| `darkroom.py` / `dream_engine.py` | 深色房调试 / 自动 dream |
| `utils.py` | 配置加载、`LLM_PRICING`、`estimate_llm_cost`、`auto_merge` |

`reflection.legacy_daily_memory_paused=true` 是旧日印象、旧自动记忆和旧每日活动汇总的硬暂停闸；它优先于运行时覆盖文件里的旧开关。旧数据与配置保留，关系整理和记忆 enrichment/backfill 仍可继续运行。新日回顾完全走 `daily_review.*` 与独立表。

### `hold` 结构化成功结果

`server.py` 的 MCP `hold` 在成功新建或合并后统一返回 `{status, action, bucket_id, bucket_name}`，其中 `action` 为 `created` 或 `merged`。会话执行器只有在 `status=success, action=created` 时才应把 `bucket_id` 记入本窗口新建桶排除账本；年轮由 `comment_bucket` 单独写入。

### feel / whisper 写入与读取

无源第一人称感受用 `hold(feel=True)` 创建不带 `whisper` 标签的独立 `type=feel` 桶；已有记忆的新感受用 `comment_bucket(kind="feel")` 写成年轮，`hold(feel=True, source_bucket=...)` 会拒绝并提示改用年轮。`whisper=True` 仅保留旧客户端兼容。`breath(domain="feel")` 排除日印象和 whisper，按创建时间倒序返回，并同时受 `max_results`（默认 20）与 `max_tokens`（默认 10000）限制；历史 whisper 只能经 `domain="whisper"` 显式读取。

`breath(domain="pinned")` 是钉选桶专用读取入口，不受普通 `max_results` 条数限制；一次调用按 `max_tokens` 预算公平分配正文，并列出全部能够放入预算的钉选桶 ID、标题和内容。它不混入 protected、自我锚点或 journey。

### 事件时间与日期读取

`hold(date=...)` 写入 `metadata.event_time`，通常为完整 ISO 时间。`breath(date=...)` 按北京时间年月日匹配，并以 `event_time` 为事实源；只有缺少 `event_time` 时才兼容旧 `metadata.date`，两者都缺失的旧桶才回退到 `created`、`updated_at` 或 `last_active`。日期结果的排序与日期标签使用同一优先级，不能因实际建桶日期误命中事件桶。启动迁移只补缺失的 `event_time`，已有值不覆盖；补值时先取旧 `date`，再取 `created`。

### journey 隔离与读取

`domain=["journey"]` 的桶属于独立 `relationship_journey` 记忆层。普通关键词、向量、日期、词法补召、开窗浮现、写入合并候选和 bucket/moment 关联扩散均排除 journey；dashboard `/api/search` 为人工管理显式放行，但普通记忆库前端会过滤 journey，独立关系轨迹页使用专用接口读取。`breath(domain="journey")` 只返回按阶段起始时间倒序排列的精简目录（桶 ID、阶段标题、起止时间、一句摘要），选择后再用 `read_bucket(bucket_id)` 读取全文。目录优先使用结构化阶段元数据，旧桶缺失时回退到事件/创建时间与正文第一条有效行。

普通 MCP 写入口不能维护 journey：`hold(journey=True)`、`hold(domain="journey")`、`comment_bucket`、`delete_bucket_comment` 和 `trace` 对 journey 均返回拒绝；通用 Dashboard 新建入口也拒绝 journey。独立关系轨迹页通过认证的 `/api/journeys*` 读取与人工纠错，证据只维护阶段级 `journey_source_bucket_ids`；`read_bucket` 会附带证据桶名称与 ID。后台使用 `BucketManager.create_journey_stage()`、`append_open_journey_stage()` 和 `close_open_journey_stage()` 管理状态。新阶段写 `journey_status=open`，同一时间只允许一个开放阶段；关闭后写 `journey_status=closed` 与 `journey_end`。`append_open_journey_stage()` 保留兼容名称，但现在把模型给出的整合后完整正文替换进开放阶段，不再字符串追加。可传 `operation_id` 幂等去重；旧 journey 不自动迁成开放状态。

### 每周 journey 候选与人工批准

`automation_schedules`、`automation_runs`、`automation_candidates` 存在独立 `state/automations.sqlite`，不写成普通 bucket。持久调度器每 30 秒检查到期任务，并用任务 lease 防止并发重复领取；设置页可调整日回顾启停/时分与 weekly journey 启停/星期/时分/协作者，时区固定 `Asia/Hong_Kong`，日界线固定 04:00。默认日回顾每日 04:30，weekly journey 周一 05:00，后者错开半小时等待周日日回顾完成。

`weekly_journey` 的星期/时分只决定触发频率，材料范围从所选协作者的 `reviewed_through_date` 下一天 04:00 连续读取到最近已经完整结束的 OB 日；积压超过 31 天时先处理最早一段，不跳过后续日期。新桶看 `metadata.created`，独立 feel 排除 whisper 以及 `daily_impression` / `weekly_impression` / `relationship_weather`，旧桶新增 feel 年轮看 `comments[].created`，最终按 bucket ID 去重。日回顾使用当前 Haven `profile_id` 与明确协作者按日期范围读取。同一协作者已有 pending/applying/failed 候选时不再次调用模型。

候选只允许 `no_change`、`append_current`、`transition`，证据 ID 必须来自固定输入快照；`append_current` 的 draft 使用 `revised_content`，表示去重、压缩并整合旧正文后的完整开放阶段正文，默认最多 5000 字符。同一 `cycle_key + input_hash` 重试回放同一 run/candidate。编辑只替换 draft 并增加 revision，原始 preview 保留；确认请求只提交 `expected_revision + approved_payload_hash`，服务端重新规范化并冻结完整 approved payload/hash，浏览器不能临时提交另一份正文。

`automation_executor.py` 只注册 `weekly_journey`。首次确认前会校验候选仍 pending、已梳理截止游标、开放 journey 完整快照、批准稿 hash 和证据桶存在且非 journey；冲突保存结构化原因，不覆盖当前阶段。同一任务的持久 lease 串行化并发确认。`no_change` 零 journey 写入但人工确认后仍推进截止游标；`append_current` 用 `revised_content` 重写开放阶段；`transition` 以稳定的 `:close` / `:create` 派生 operation ID 两步执行。候选完成与游标推进在同一 SQLite 事务；失败、拒绝、冲突和仅生成候选均不推进。关闭成功而创建失败时保留部分结果，重试幂等回放 close 后继续 create。

## 配置

```yaml
# config.yaml 关键项（完整见 config.example.yaml）
buckets_dir: "./buckets"
merge_threshold: 75
auto_merge: true    # OMBRE_AUTO_MERGE=false 可关闭
matching:
  fuzzy_threshold: 50
  max_results: 5
scoring_weights:
  topic_relevance: 4.0
  emotion_resonance: 2.0
  time_proximity: 1.5
  importance: 1.0
  content_weight: 1.0       # 正文权重（运行时旋钮可覆盖）
  title_hit_bonus: 0.0      # 标题命中加分（运行时旋钮可覆盖）
  keyword_first_sort: false # 标题命中排最前（运行时旋钮可覆盖）
  precise_match_mode: false # 严格关键词匹配（运行时旋钮可覆盖）
  warmth_boost: 0.0         # 温暖偏置（运行时旋钮可覆盖）
```

## 环境变量

完整清单见 **ENV_VARS.md**。核心项：

```
OMBRE_API_KEY=             # LLM API key（必须）
OMBRE_BASE_URL=            # LLM API 地址
OMBRE_TRANSPORT=           # stdio / streamable-http
OMBRE_BUCKETS_DIR=         # 存储目录
OMBRE_AUTO_MERGE=          # true/false，关闭自动合并
OMBRE_SCORING_WARMTH_BOOST= # 温暖偏置初始值
```

## REST API

**完整路由以代码为准**（`server.py` 的 `@mcp.custom_route` 90+ 条 + `gateway.py` 的 `Route()`），用 `grep -oE "@mcp\.custom_route\(\"[^\"]*\"" server.py` 实时查。下面列出 dashboard 前端实际消费的核心组：

### 认证
```
POST /auth/login  { password } → set-cookie
```

### 桶 CRUD
```
GET    /api/buckets                          # 所有桶（含 noise 字段）
GET    /api/bucket/{bucket_id}               # 单个桶（含 noise 字段）
POST   /api/bucket                           # 新建
PATCH  /api/bucket/{bucket_id}               # 更新（支持 noise 标记），成功返回重载后的 bucket
DELETE /api/bucket/{bucket_id}               # 软删除 → 回收站
POST   /api/touch/{bucket_id}?ripple=true/false     # 轻触/激活
POST   /api/archive/{bucket_id}              # 归档
POST   /api/unarchive/{bucket_id}            # 恢复归档
POST   /api/bucket/{bucket_id}/comments      # 评论
DELETE /api/bucket/{bucket_id}/comments/{comment_id}
```

### 回收站
```
GET  /api/trash                   # 列表
POST /api/trash/empty             # 清空
POST /api/bucket/{bucket_id}/restore     # 恢复
POST /api/bucket/{bucket_id}/purge       # 彻底删除（物理 os.remove）
```

### 搜索
```
GET /api/search?q=&simulate=&include_vector=&include_noise=&include_archive=&limit=&show_all=
# simulate=true → 返回 matched_fields（title/domain/tags/content 匹配详情）
# include_vector=true → 附加 vector_similarity
# include_noise=true → 包含噪声桶
# record_stats 由后端控制（simulate 时不记录）
GET /api/search-raw   # 原文检索（GET / JSON POST）；usage_scope=runtime|historical_archive|all
GET /api/raw-conversations             # 历史档案窗口目录；只读 historical_archive
GET /api/raw-conversation-events       # 单窗口按时间正序分页；只读 historical_archive，metadata.thinking 独立于正文
```

### 相似 & 合并
```
GET  /api/bucket/{bucket_id}/similar?n=5                 # embedding 相似桶
POST /api/bucket/{bucket_id}/merge-preview?into={id}     # LLM 合并预览 + 费用估算
POST /api/bucket/{bucket_id}/merge-commit?into={id}      # 确认合并（更新 B，删除 A）
```

### 可观测性
```

### Gateway / cc 会话持久化
```
POST   /gateway/api/conversation/turn
       # 兼容旧写入；携带 request_id + expected_last_round_id + persona_id 时
       # 使用原子 compare-and-append，并可同轮绑定 attachment_ids、记录 recalled_bucket_ids / created_bucket_ids
GET|POST|DELETE /gateway/api/conversation/attachment
       # 上传压缩图片、Bearer 私有读取、清除单张或当前窗口全部图片
GET    /gateway/api/conversation/turn?request_id=
       # 按 profile + request_id 读回已提交轮次及 raw_json/persona_id，供调用端持久幂等重放
GET    /gateway/api/conversation/turns?session_id=&after_round_id=&source=
       # 读取窗口历史；after_round_id 供各 CC 线路补齐未见的跨线路文字轮次
GET    /gateway/api/conversation/sessions?source=&persona_id=&deleted=1
       # 默认只列活动窗口；deleted=1 只列软删除窗口，供前端永久删除区使用
GET    /gateway/api/conversation/session?session_id=&include_bucket_exclusions=1
       # 窗口归属、闲聊/工作模式、固定日回顾与 handoff 快照、引擎/提示词覆盖、CC 分线路 session/游标、Context GC 状态与可选桶排除集合
PATCH  /gateway/api/conversation/session
       # 修改持久窗口覆盖；context_gc_preferences 保存自动开关/保护项，context_gc_commit 用 state_version + 旧 cc_session_id 原子切换减负副本并写历史
DELETE /gateway/api/conversation/session
       # 默认软删除；permanent=true 且 confirm_session_id 精确匹配时永久删除窗口数据
GET|PATCH /gateway/api/daily-reviews?persona_id=
       # 独立日回顾列表与手动微调；不进入 bucket、搜索或召回
```
GET  /api/hit-stats?limit=&include_zero=&order=&exclude_gated=   # 命中统计
POST /api/hit-stats/reset                                       # 重置
GET  /api/recent-searches?limit=                                # 检索追溯
GET  /api/scoring-config                                        # 读评分旋钮
POST /api/scoring-config                                        # 写旋钮（持久化 runtime_config.json）
POST /api/scoring-config/reset                                  # 重置为默认值
GET  /api/breath-debug?q=&valence=&arousal=&threshold=          # 模拟 breath（亦记录命中统计）
GET  /api/recall-debug                                           # 召回调试
GET  /api/status                                                 # 状态
```

### 日记
```
GET  /api/journal                        # 列表（60s 内存缓存）
POST /api/journal                        # 新建（自动 invalidate 缓存）
GET|PATCH|DELETE /api/journal/{journal_id} # 专属详情、完整编辑与删除
POST /api/bucket/{bucket_id}/to-journal  # 桶转日记（不可逆）
```

### 关系轨迹
```
GET   /api/journeys                      # 阶段目录，兼容旧 journey 缺失字段
GET   /api/journeys/{bucket_id}          # 完整正文、阶段字段与证据桶名称/ID
PATCH /api/journeys/{bucket_id}          # 认证人工纠错；校验唯一 open 与证据桶
```

### 自动化候选
```
GET  /api/automations/status?task_type=weekly_journey|daily_review
PATCH /api/automations/schedule                    # 持久调整 weekly journey 启停/星期/时分/协作者/已梳理截止日
PATCH /api/automations/execution                   # 分别保存 daily_review / weekly_journey 的 API/Pro 与 Pro 模型
POST /api/automations/weekly-journey/run       # 手动生成 pending 候选；不写 journey
GET  /api/automations/candidates?task_type=weekly_journey&status=pending
GET  /api/automations/candidates/{candidate_id}
PATCH /api/automations/candidates/{candidate_id}          # expected_revision + draft；保存新 revision
POST /api/automations/candidates/{candidate_id}/reject    # 只拒绝 pending 候选，零 journey 写入
POST /api/automations/candidates/{candidate_id}/confirm   # expected_revision + approved_payload_hash
```

### 导入
```
POST /api/import/upload?mode=large|small&max_chunks=N   # 启动导入
GET  /api/import/status                                 # 进度（含 cost/tokens）
GET  /api/import/results?limit=                          # 最近导入结果
POST /api/import/review                                  # 审查决策（important/pin/noise/delete）
POST /api/import/pause                                   # 暂停
GET  /api/import/patterns                                # 模式检测
```

### 配置 & 记忆
```
GET  /api/config                      # 读取模型、自动打标运行参数、召回等 Dashboard 安全配置
POST /api/config                      # 热更新并可持久化模型、自动打标运行参数、召回等配置；密钥写 state/.env
GET  /api/prompts                     # 读 prompt
POST /api/prompts                     # 按 revision 持久保存产品 Prompt，下一次调用立即生效
POST /api/prompts/reset               # 删除用户覆盖并恢复当前代码默认
POST /api/prompts/test                # analyze/merge 局部草稿试跑；不改共享实例、不持久化
GET  /api/todos / POST /api/todos / POST /api/todos/{id}/writeback   # 待办
GET  /api/reminders / POST /api/reminders / DELETE /api/reminders/{id}  # 照顾备忘
GET  /api/persona / GET /api/portrait-state*                        # 画像
GET  /api/moments / GET /api/edges / GET /api/word-map*              # 记忆图；单桶 moments 返回桶内边与带目标桶名称的跨桶边
POST /api/ingest-raw / POST /api/memories                            # 原文写入；前者兼容历史档案查重/分块归档动作
GET  /api/daily-chat-memory/pending | /run | /confirm                # 每日聊天记忆
GET|PATCH /api/daily-reviews                                            # 日回顾列表 / 手动微调
POST /api/daily-reviews/run                                             # 指定日期手动生成日回顾
```

### Hooks & 调试
```
GET /breath-hook                      # SessionStart hook（自动 breath）
GET /dream-hook                       # 自动 dream
GET /introspection-hook               # 自省 hook
GET /api/debug/injections             # 注入调试（见 README「Gateway 注入边界」）
```

---

## 关键实现细节

### 召回必要性 Shadow

`recall_policy.py` 的 `RecallNecessityPlan` 在候选桶相关性之前独立判断 `none / explicit / contextual`，并用 `targetable` 防止“你还记得吗”这类无目标请求扩大检索。召回否定/复盘和 Shadow 测试语境优先为 `none`；同轮组合表达召回测试、召回观测和“不用搜/不用回忆”时，以 `recall_test_observation_search_negated` 在明确回忆与自然话题之前判 `none`，但真正指向过去测试事件的“你还记得……”仍判 `explicit`；无触发词但有具体自然话题也可为 `contextual`。`gateway.py` 保留原 admission gate、排序和正式注入，只在 `phase1_recall_shadow_enabled=true` 时对正式/被拒候选统一执行 shadow relevance：具体话题须得到语义或唯一直接证据支持，普通 keyword-only 候选不能靠 `first_card_min_score=0.55` 证明相关。Planner degraded 时 contextual 可删除正式噪声但不得新增桶。通过 relevance 的候选再进入代码版 `promote / neutral / reject` utility：明确回忆和有可用上一轮上下文的接续指代优先，自然 contextual 无法确定增量价值时保持 neutral 且仍可入选，只有确定无增量的完全重复才拒绝；Shadow 最终最多投影一张卡。候选 `semantic_status` 区分 scored、未进 Top K、embedding 缺失/过期和查询不可用；necessity、relevance、utility 与单卡结果均标记为不影响正式召回。

### 噪声系统
噪声 = `resolved=true AND importance=1`。标记时写入 `importance_before_noise` 备份；撤销时自动恢复。`search()` 默认排除，`include_noise=true` 可包含。各 API 响应含 `"noise": bool` 字段。

### 回收站
软删除流程：`delete()` → 写 `original_type` + `trashed_at` → `shutil.move` 到 `buckets/trash/`。`_find_bucket_file()` 搜索 trash 目录。恢复/彻底删除/清空均通过独立方法。

### 命中统计
`buckets/hit_stats.json` 持久化。`search()` 调用 `record_hit()` 记录（debounce 10 次或强制刷新）。`api_breath_debug` 有 query 时也记录。`record_surface_trace()` 记录 breath 无查询浮现。

### 检索评分旋钮
`runtime_config.json["scoring"]` 持久化，启动时加载。全部默认值 = 跟上游行为一致。通过 `apply_runtime_scoring_overrides()` 即时生效。

### 产品 Prompt 持久化与硬约束
`state/prompt_overrides.sqlite` 只保存用户自定义产品层，不复制代码默认；不存在覆盖时始终读取当前版本的系统默认。表按 `profile_id + name` 隔离，初始化和旧表补列可重复执行，保存使用 revision 检查跨窗口冲突。四个白名单名称为 `analyze`、`merge`、`daily_review`、`weekly_journey`。

生成时按“协作者基础提示词/默认模块 + 可配置产品层 + 服务端硬约束 + 固定材料”组装。自动打标的 JSON/字段/domain/保留标签、记忆合并的 section/身份/正文约束、日回顾的材料与独立表边界、weekly journey 的 JSON/三类候选/固定证据/实质变化/不得编造/零自动写入/revision-hash 均不可由自定义正文替换。`GET /api/prompts` 同时返回运行时叠加说明、实际模型硬约束全文和模型返回后的服务端校验摘要，供 dashboard 只读展示完整分层。自动打标与记忆合并共用 dehydration 的 `max_tokens`、`temperature`、`thinking_mode`，经 `/api/config` 热更新并写入 runtime overlay；`analyze` 和 `merge` 的测试通过局部 Prompt 参数试跑，不再临时改写全局 `Dehydrator` 属性。日回顾与 weekly journey 不提供会污染正式表或候选的测试入口。

### 中文分词
`jieba` 分词（`_split_query_tokens()`），自动切长句。内置 stopword 过滤。`precise_match_mode` 开启时从 `partial_ratio` 切换到精确子串匹配。

### 双重打分模式
- **默认**（`precise_match_mode=false`）：四维加权（topic + emotion + time + importance）+ 温暖偏置 + 标题加分
- **精确模式**（`precise_match_mode=true`）：纯关键词 token 命中计数，砍掉 emotion/time/importance

### 关键词命中优先
当 query 的 token 精确匹配了桶的 name 或 domain 时，即使综合分未过 `fuzzy_threshold` 也强制通过（normalized 设为 threshold × 0.7）。

### auto_merge 控制
`OMBRE_AUTO_MERGE=false` 时 `_merge_or_create()` 跳过合并，始终新建桶。用于手动合并工作流。

### Journal 缓存
`_JOURNAL_CACHE` 60s TTL。新建、编辑、删除日记时均 `_invalidate_cache("JOURNAL")`。Journal 使用独立目录和专属 GET/PATCH/DELETE；标题、正文、作者、`event_time`、锁定状态可编辑，`created` 不随编辑改变。

### LLM 成本追踪
`utils.estimate_llm_cost()` 支持 18 个模型。导入和合并预览返回 cost/cny/token 用量。

### 相似记忆
依赖 embedding 引擎（`config.yaml` 中 `embedding.enabled`）。返回 `{items, embedding_enabled, total_scanned}`。

### cc 持久化（Haven 侧）
cc 配置/用户数据由 **Gateway** 持久化到 Haven 数据库，路由注册在 `gateway.py`（~21441 行 `Route()` 列表）：
```
/api/cc/personas      # 协作者（含 dirs/write_dirs 与 selfhost_defaults；密钥硬拦）
/api/cc/upstream      # 上游模型配置（cc_upstream_config 表）
/api/cc/permissions   # 写权限批准
/api/cc/mcp           # MCP 工具配置
/api/cc/pro-usage-snapshot  # 当前 profile 最近一次 Pro 额度快照；GET / POST 单条覆盖
```
dashboards 的 `/api/gateway/[...path]` 代理到这些路由，Bearer 网关鉴权。

会话轮次存 `conversation_turns`，窗口状态存 `conversation_sessions`，图片/文件元数据与文件解析正文存 `conversation_attachments`；私有文件位于 `buckets_dir/cc-attachments`：

- 订阅、API 中转站和 selfhost 共用的协作者基础提示词存 `cc_personas.base_prompt`，默认值为原 cc 闲聊模式提示词；短暂使用过的旧 selfhost 三句默认文案在读取时迁成该统一默认。提示词模块存 `cc_personas.prompt_modules`，每条包含 id、名称、正文和默认启停，组装时以 `【模块名称】` 标明边界。旧 `prompt` 在读取时兼容成一个默认开启模块。当前窗口的差异化启停存 `conversation_sessions.prompt_module_overrides_json`，未覆盖的模块继续跟随协作者默认。
- 一个 `session_id` 永久绑定一个 `persona_id`；旧窗口从首轮 `client="ob2-chat/<persona>"` 回填，无主历史归 `ombre`。
- `local_engine_preference` 只保存用户的本地首选；Vercel 的 `effective_engine=selfhost` 不得写回。
- `handoff_snapshot_json` 按 `profile_id + session_id` 保存 Dashboard 已完成统一预算裁剪的换窗正文与统计；只接受首次写入，后续轮次和幂等重试不得覆盖。CC 每条原生线路启动时与无状态 selfhost 每轮读取同一快照，避免切引擎、重启或换设备后丢失钉选桶、最近记忆、feel、journal、日回顾或旧聊天原文。
- `frozen_persona_append` 按 `profile_id + session_id` 首次写入后冻结，保存 CC 实际系统提示词追加前缀；Dashboard 重部署或换设备后继续读取原值，窗口永久删除时随 `conversation_sessions` 一起删除。`frozen_persona_append_initialized` 区分“尚未写入”和“已冻结为空串”，旧库迁移可重复执行。
- `cc_pro_usage_snapshot` 每个 profile 只保留最近一条 Claude Pro 额度快照，新读取覆盖旧值，不按窗口累积；Dashboard 无在线 Pro 子进程时把它作为带时间戳的上次值显示。
- 严格写入用 `request_id` 防重复，用 `expected_last_round_id` 拒绝基于旧历史的跨设备追加；SQLite `BEGIN IMMEDIATE` 内统一分配下一轮。
- 附件先按窗口暂存，严格写入把有序 ID + SHA-256 纳入幂等指纹并在同一事务绑定轮次；图片接受 JPEG/PNG/WebP（压缩后单张不超过 2MB），文件接受 PDF/DOCX/MD/TXT/CSV（单个不超过 4MB，并保存浏览器提取的受限正文），每轮两类合计不超过 4 个。私有读取必须经 Bearer 网关，未绑定附件 24 小时后在后续上传时清理；按 `kind=image/file` 分类清除互不影响，文件清除同时擦除解析正文。
- `/api/conversation/turn?request_id=` 可在进程重启或换设备后读回严格写入结果；调用端校验 session/persona/user 原文后重放已保存过程，不再请求上游。
- `cc_overrides_json` 保存当前 CC Pro/API 路由及各自模型、力度、thinking 和 API provider 选择；`cc_lanes_json` 按 `subscription` / `api:<provider_id>` 分别保存 Claude 原生 `cc_session_id` 与 `seen_round_id`。只有该线路的 CC 轮次严格写入成功才推进自身游标；旧 `cc_seen_round_id` 仅保留兼容。
- `context_gc_json` 按窗口保存默认关闭的 05:30 自动开关、始终保留 key、最近 20 次释放估算和旧/新 Claude session 指针。减负只更新 `cc_lanes_json` 指针与 GC 日志，不复制或改写 `conversation_turns`；提交必须同时命中 `state_version` 和旧 `cc_session_id`，否则冲突失败。
- 已召回桶继续落 `injected_buckets`；本窗口新建桶落 `session_created_buckets`，二者并集为该 session 的排除集合。召回冷却读取 `injected_at` 时把旧无时区值与新 UTC-aware 值统一按 UTC 计算，避免混合时间格式导致 hook recall 500。
- 永久删除会先删除该窗口附件文件，再清理带 `profile_id` 的窗口数据，不删除长期记忆桶；旧的无 profile 诊断/冷却表暂不清理。

---

## 调试 / 常用命令

```bash
# 重置命中统计
curl -X POST https://foryan.zeabur.app/api/hit-stats/reset \
  -H "Cookie: $(curl -s -X POST https://foryan.zeabur.app/auth/login \
    -H 'Content-Type: application/json' \
    -d '{"password":"<OMBRE_SESSION>"}' -i | grep set-cookie | cut -d';' -f1 | cut -d' ' -f2)"

# 测试搜索（含匹配详情）
curl "https://foryan.zeabur.app/api/search?q=今天&simulate=true" \
  -H "Cookie: ..."

# 重置评分旋钮为默认值
curl -X POST https://foryan.zeabur.app/api/scoring-config/reset

# 查看配置
curl https://foryan.zeabur.app/api/config

# 实时查全量路由
grep -oE "@mcp\.custom_route\(\"[^\"]*\"" server.py
```

---

## 后续事项归档

已排入后续窗口的工作维护在对应 handoff；短期不处理、没有明确排期的待删、冗余和遗留项维护在 dashboard 仓库 **`TECH_DEBT.md`**。本文件不维护副本。
