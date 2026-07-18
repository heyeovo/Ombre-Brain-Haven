@AGENTS.md
# Ombre-Brain 开发文档

> 供新窗口快速了解后端全貌，开窗口时 fetch 此文件。

## 项目概述

Ombre Brain 是 AI 长期情绪记忆系统后端。Python FastMCP + Starlette，部署在 Zeabur。前端 ob-dashboard2（Next.js 15）部署在 Vercel。

- **仓库**：github.com/heyeovo/Ombre-Brain
- **Zeabur 域名**：https://forxiaoyan.zeabur.app
- **前端仓库**：github.com/heyeovo/ob-dashboard2

## 技术栈

- **语言**：Python 3.10+
- **框架**：FastMCP + Starlette（HTTP 模式）
- **关键依赖**：jieba（中文分词）、httpx（LLM API 调用）、PyYAML
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
| `server.py` | 入口，MCP 工具注册 + REST API |
| `bucket_manager.py` | 桶 CRUD、搜索、评分、回收站、命中统计、分词 |
| `dehydrator.py` | LLM 脱水、合并、打标（含 `_last_merge_usage` 成本追踪） |
| `decay_engine.py` | 衰减引擎，计算 score |
| `embedding_engine.py` | 向量嵌入 + 相似度搜索 |
| `import_memory.py` | 对话历史导入引擎（含成本追踪） |
| `utils.py` | 配置加载、`LLM_PRICING`、`estimate_llm_cost`、`auto_merge` |

## 配置

```yaml
# config.yaml 关键项
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

```
OMBRE_API_KEY=             # LLM API key（必须）
OMBRE_BASE_URL=            # LLM API 地址
OMBRE_TRANSPORT=           # stdio / streamable-http
OMBRE_BUCKETS_DIR=         # 存储目录
OMBRE_AUTO_MERGE=          # true/false，关闭自动合并
OMBRE_SCORING_WARMTH_BOOST= # 温暖偏置初始值
```

## REST API 完整列表

### 认证
```
POST /auth/login  { password } → set-cookie
```

### 桶 CRUD
```
GET    /api/buckets                          # 所有桶（含 noise 字段）
GET    /api/bucket/{id}                      # 单个桶（含 noise 字段）
POST   /api/bucket                           # 新建
PATCH  /api/bucket/{id}                      # 更新（支持 noise 标记）
DELETE /api/bucket/{id}                      # 软删除 → 回收站
POST   /api/touch/{id}?ripple=true/false     # 轻触/激活
POST   /api/archive/{id}                     # 归档
POST   /api/unarchive/{id}                   # 恢复归档
```

### 回收站
```
GET  /api/trash                   # 列表
POST /api/trash/empty             # 清空
POST /api/bucket/{id}/restore     # 恢复
POST /api/bucket/{id}/purge       # 彻底删除（物理 os.remove）
```

### 搜索
```
GET /api/search?q=&simulate=&include_vector=&include_noise=&include_archive=&limit=&show_all=
# simulate=true → 返回 matched_fields（title/domain/tags/content 匹配详情）
# include_vector=true → 附加 vector_similarity
# include_noise=true → 包含噪声桶
# record_stats 由后端控制（simulate 时不记录）
```

### 相似 & 合并
```
GET  /api/bucket/{id}/similar?n=5                 # embedding 相似桶
POST /api/bucket/{id}/merge-preview?into={id}     # LLM 合并预览 + 费用估算
POST /api/bucket/{id}/merge-commit?into={id}      # 确认合并（更新 B，删除 A）
```

### 可观测性
```
GET  /api/hit-stats?limit=&include_zero=&order=&exclude_gated=   # 命中统计
POST /api/hit-stats/reset                                       # 重置
GET  /api/recent-searches?limit=                                # 检索追溯
GET  /api/scoring-config                                        # 读评分旋钮
POST /api/scoring-config                                        # 写旋钮（持久化 runtime_config.json）
POST /api/scoring-config/reset                                  # 重置为默认值
GET  /api/breath-debug?q=&valence=&arousal=&threshold=          # 模拟 breath（亦记录命中统计）
```

### 日记
```
GET  /api/journal                        # 列表（60s 内存缓存）
POST /api/journal                        # 新建（自动 invalidate 缓存）
POST /api/bucket/{id}/to-journal         # 桶转日记（不可逆）
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

### 配置
```
GET  /api/config                      # { fuzzy_threshold, max_results }
POST /api/config { fuzzy_threshold }  # 更新（重启恢复）
GET  /api/prompts                     # 读 prompt
POST /api/prompts                     # 写 prompt
POST /api/prompts/test                # 测试 prompt
```

### Hooks
```
GET /breath-hook                      # SessionStart hook（自动 breath）
GET /dream-hook                       # 自动 dream
```

---

## 关键实现细节

### 噪声系统
噪声 = `resolved=true AND importance=1`。标记时写入 `importance_before_noise` 备份；撤销时自动恢复。`search()` 默认排除，`include_noise=true` 可包含。各 API 响应含 `"noise": bool` 字段。

### 回收站
软删除流程：`delete()` → 写 `original_type` + `trashed_at` → `shutil.move` 到 `buckets/trash/`。`_find_bucket_file()` 搜索 trash 目录。恢复/彻底删除/清空均通过独立方法。

### 命中统计
`buckets/hit_stats.json` 持久化。`search()` 调用 `record_hit()` 记录（debounce 10 次或强制刷新）。`api_breath_debug` 有 query 时也记录。`record_surface_trace()` 记录 breath 无查询浮现。

### 检索评分旋钮
`runtime_config.json["scoring"]` 持久化，启动时加载。全部默认值 = 跟上游行为一致。通过 `apply_runtime_scoring_overrides()` 即时生效。

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
`_JOURNAL_CACHE` 60s TTL。新建日记时 `_invalidate_cache("JOURNAL")`。

### LLM 成本追踪
`utils.estimate_llm_cost()` 支持 18 个模型。导入和合并预览返回 cost/cny/token 用量。

### 相似记忆
依赖 embedding 引擎（`config.yaml` 中 `embedding.enabled`）。返回 `{items, embedding_enabled, total_scanned}`。

---

## 调试 / 常用命令

```bash
# 重置命中统计
curl -X POST https://forxiaoyan.zeabur.app/api/hit-stats/reset \
  -H "Cookie: $(curl -s -X POST https://forxiaoyan.zeabur.app/auth/login \
    -H 'Content-Type: application/json' \
    -d '{"password":"<OMBRE_SESSION>"}' -i | grep set-cookie | cut -d';' -f1 | cut -d' ' -f2)"

# 测试搜索（含匹配详情）
curl "https://forxiaoyan.zeabur.app/api/search?q=今天&simulate=true" \
  -H "Cookie: ..."

# 重置评分旋钮为默认值
curl -X POST https://forxiaoyan.zeabur.app/api/scoring-config/reset

# 查看配置
curl https://forxiaoyan.zeabur.app/api/config
```

---

## 未接入功能（规范列表 — ob-dashboard2 引用此列表）

- [ ] 重新脱水（redehydrate）— Fork 有 /api/bucket/{id}/redehydrate + redehydrate-commit
- [ ] 控制台配置页 — 多组 LLM profile、衰减权重 UI 调节
- [ ] 自动备份 — GitHub Actions 每天备份 buckets 到私有仓库
- [ ] 情感唤起罗盘 — 手机端 2D 心情坐标选记忆 + LLM 叙事
