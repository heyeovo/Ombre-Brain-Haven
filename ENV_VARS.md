# 环境变量参考

所有环境变量均为可选。推荐只设置前 4 个核心密钥变量，其余配置通过 `config.yaml` 管理（支持 Dashboard 热更新，重启不丢失）。

## 核心密钥（建议设置）

| 变量名 | 默认 | 说明 |
|--------|------|------|
| `OMBRE_API_KEY` | — | 脱水/打标 LLM 的 API Key（覆盖 `dehydration.api_key`） |
| `OMBRE_EMBEDDING_API_KEY` | — | 向量嵌入 API Key（覆盖 `embedding.api_key`；留空复用 `OMBRE_API_KEY`） |
| `OMBRE_GATEWAY_TOKEN` | — | Gateway 的 Bearer 认证令牌 |
| `OMBRE_DASHBOARD_PASSWORD` | — | Dashboard 预设密码；设置后首次访问不弹设置向导，页面内"修改密码"禁用 |

## 脱水 / 打标模型

`config.yaml` 字段：`dehydration.*`

| 变量名 | 覆盖的 YAML 键 | 说明 |
|--------|----------------|------|
| `OMBRE_DEHYDRATION_MODEL` | `dehydration.model` | 脱水/打标/合并/拆分 LLM 模型（默认 `deepseek-chat`） |
| `OMBRE_MODEL` | 同上 | `OMBRE_DEHYDRATION_MODEL` 的别名，前者优先 |
| `OMBRE_DEHYDRATION_BASE_URL` | `dehydration.base_url` | 脱水 API Base URL |
| `OMBRE_BASE_URL` | `dehydration.base_url` | 同上（`OMBRE_DEHYDRATION_BASE_URL` 优先） |

## 向量嵌入

`config.yaml` 字段：`embedding.*`

| 变量名 | 覆盖的 YAML 键 | 说明 |
|--------|----------------|------|
| `OMBRE_EMBEDDING_MODEL` | `embedding.model` | 向量模型（默认 `Qwen/Qwen3-Embedding-4B`） |
| `OMBRE_EMBEDDING_BASE_URL` | `embedding.base_url` | 向量 API Base URL |
| `OMBRE_EMBEDDING_ENABLED` | `embedding.enabled` | 启用/关闭向量检索（`true`/`false`） |
| `OMBRE_EMBEDDING_MAX_CHARS` | `embedding.max_chars` | 单次向量化最大字符数（默认 6000） |
| `OMBRE_EMBEDDING_QUERY_INSTRUCTION` | `embedding.query_instruction` | 向量查询指令 prompt |

## 重排序

`config.yaml` 字段：`reranker.*`

| 变量名 | 覆盖的 YAML 键 | 说明 |
|--------|----------------|------|
| `OMBRE_RERANKER_MODEL` | `reranker.model` | 重排序模型 |
| `OMBRE_RERANKER_BASE_URL` | `reranker.base_url` | 重排序 API Base URL |
| `OMBRE_RERANKER_API_KEY` | `reranker.api_key` | 重排序 API Key |
| `OMBRE_RERANKER_ENABLED` | `reranker.enabled` | 启用/关闭重排序 |

## Gateway

`config.yaml` 字段：`gateway.*`

| 变量名 | 覆盖的 YAML 键 | 说明 |
|--------|----------------|------|
| `OMBRE_GATEWAY_HOST` | `gateway.host` | 监听地址 |
| `OMBRE_GATEWAY_PORT` | `gateway.port` | 监听端口 |
| `OMBRE_GATEWAY_UPSTREAM_BASE_URL` | `gateway.upstream_base_url` | 上游 API 地址 |
| `OMBRE_GATEWAY_UPSTREAM_MODEL` | `gateway.upstream_default_model` | 默认上游模型 |
| `OMBRE_GATEWAY_UPSTREAM_MODELS` | `gateway.upstream_models` | 可用模型列表（逗号分隔） |
| `OMBRE_GATEWAY_UPSTREAM_API_KEY` | — | 旧版单 upstream 的兜底 API Key；仅注入 Gateway，不得注入 Brain / Dashboard。多 upstream 优先使用各自的 `api_key_env` |
| `OMBRE_GATEWAY_ADMIN_URL` | — | Gateway 管理 API 地址 |
| `OMBRE_GATEWAY_DEBUG_TIMEOUT_SECONDS` | — | Gateway debug 请求超时（默认 30s） |
| `OMBRE_DOMAIN_SENTINEL_MODEL` | `gateway.domain_sentinel_model` | 领域哨兵模型 |
| `OMBRE_DOMAIN_SENTINEL_API_KEY` | — | 领域哨兵 API Key |

> `OMBRE_GATEWAY_TOKEN` 用于客户端 Bearer 认证和 Gateway Debug 端点访问。各 upstream 的 API key 通过 `gateway.upstreams[*].api_key_env` 分别指向独立环境变量。

## Persona / Reflection / Daily Review / Portrait / Dream

`config.yaml` 字段：`persona.*` / `reflection.*` / `daily_review.*` / `portrait.*` / `dream.*`

| 变量名 | 覆盖的 YAML 键 | 说明 |
|--------|----------------|------|
| `OMBRE_PERSONA_API_KEY` | `persona.api_key` | Persona 模型密钥 |
| `OMBRE_PERSONA_BASE_URL` | `persona.base_url` | Persona 模型地址 |
| `OMBRE_PERSONA_MODEL` | `persona.model` | Persona 模型名 |
| `OMBRE_REFLECTION_API_KEY` | `reflection.api_key` | Reflection 模型密钥 |
| `OMBRE_REFLECTION_BASE_URL` | `reflection.base_url` | Reflection 模型地址 |
| `OMBRE_REFLECTION_MODEL` | `reflection.model` | Reflection 模型名 |
| `OMBRE_REFLECTION_CANDIDATE_MODEL` | `reflection.daily_chat_memory_candidate_model` | 自动记忆候选模型 |
| `OMBRE_DAILY_REVIEW_API_KEY` | `daily_review.api_key` | 日回顾模型密钥；留空时复用 Reflection 密钥 |
| `OMBRE_DREAM_API_KEY` | `dream.api_key` | Dream 模型密钥 |
| `OMBRE_DREAM_BASE_URL` | `dream.base_url` | Dream 模型地址 |
| `OMBRE_DREAM_MODEL` | `dream.model` | Dream 模型名 |
| `OMBRE_DREAM_ENABLED` | `dream.enabled` | 启用/关闭夜梦 |

### 日回顾 / 每周轨迹的 Claude Pro runner

仅当 Dashboard「自动化与状态」把 `daily_review` 或 `weekly_journey` 明确选为 Claude Pro 时需要。两项任务的选择持久化在 Haven `state/automations.sqlite`；默认仍为 API，运行失败不会自动切换引擎。

| 变量名 | 注入位置 | 说明 |
|--------|----------|------|
| `OMBRE_AUTOMATION_PRO_RUNNER_URL` | Haven Brain | Dashboard 专用 runner 的完整 URL，例如内部或受 HTTPS 保护的 `https://dashboard.example/api/automation-pro-runner` |
| `OMBRE_AUTOMATION_PRO_RUNNER_TOKEN` | Haven Brain + Dashboard | 两端相同的随机共享密钥；只用于 runner Bearer 认证，不返回浏览器、不写数据库或 Git |

### CC 主动唤醒 runner

阶段 4 起，Haven Brain 每 30 秒从 `gateway_state.db` 领取到期的 CC wake，再调用 Dashboard 的受限后台 runner。两项均为必填；缺任一项时 scheduler 不启动。

| 变量名 | 注入位置 | 说明 |
|--------|----------|------|
| `OMBRE_AGENT_WAKE_RUNNER_URL` | Haven Brain | Dashboard wake runner 的完整 URL，例如 `https://dashboard.example/api/cc-agent-wake-runner` |
| `OMBRE_AGENT_WAKE_RUNNER_TOKEN` | Haven Brain + Dashboard | 两端相同的独立随机共享密钥；只用于 wake callback Bearer 认证，不返回浏览器、不写数据库或 Git |

## 存储路径

| 变量名 | 说明 |
|--------|------|
| `OMBRE_BUCKETS_DIR` | 记忆桶文件存放目录（默认 `./buckets`；Docker Volume 挂载时务必设置） |
| `OMBRE_STATE_DIR` | 运行状态目录（默认 `<buckets_dir>/../state`），含 embedding DB、portrait 等 |
| `OMBRE_RUNTIME_CONFIG_PATH` | 运行时配置路径（默认 `<state_dir>/config.runtime.yaml`） |
| `OMBRE_ENV_PATH` | Dashboard 持久密钥文件路径（默认 `<state_dir>/.env`；进程环境变量优先于该文件） |

## 传输与网络

| 变量名 | 说明 |
|--------|------|
| `OMBRE_TRANSPORT` | MCP 传输模式：`stdio` / `sse` / `streamable-http`（默认 `stdio`） |
| `OMBRE_PORT` | HTTP/SSE 模式监听端口（默认 `8000`） |

### Coolify VPS Compose 栈固定值

`compose.coolify.test.yml` 为当前 VPS `haven-test-stack` 显式设置以下运行时值；文件名、资源名和历史挂载路径中的 `test` 为迁移兼容名称，不代表它仍是非正式数据栈：

| 变量名 | 默认 | 说明 |
|--------|------|------|
| `HAVEN_RELEASE_SHA` | — | Coolify Compose 专用的发布版本；必须填写完整 Git commit SHA。Brain 与 Gateway 共用此值，变量为空时 `${HAVEN_RELEASE_SHA:?}` 会阻止部署。该值只用于选择构建源，不注入运行中的容器 |

- Brain：`OMBRE_TRANSPORT=streamable-http`、`OMBRE_PORT=8000`、`OMBRE_BUCKETS_DIR=/data`、`OMBRE_STATE_DIR=/state`、`OMBRE_RUNTIME_CONFIG_PATH=/state/config.runtime.yaml`。
- 若启用两项自动化的 Pro 路线，Brain 还需注入 `OMBRE_AUTOMATION_PRO_RUNNER_URL` 与 `OMBRE_AUTOMATION_PRO_RUNNER_TOKEN`；Dashboard Application 只需注入同一个 `OMBRE_AUTOMATION_PRO_RUNNER_TOKEN`。
- CC 主动唤醒需为 Brain 注入 `OMBRE_AGENT_WAKE_RUNNER_URL` 与 `OMBRE_AGENT_WAKE_RUNNER_TOKEN`，Dashboard Application 注入相同的 `OMBRE_AGENT_WAKE_RUNNER_TOKEN`。
- Gateway：`OMBRE_GATEWAY_HOST=0.0.0.0`、`OMBRE_GATEWAY_PORT=8010`，并与 Brain 共用 `/data` 和 `/state`。
- Brain 通过 `OMBRE_GATEWAY_ADMIN_URL=http://haven-gateway:8010/api/config` 使用内部服务名连接 Gateway。
- 密钥变量只从 Coolify 环境注入；Compose 文件不包含真实值。上游模型密钥只允许注入 Gateway，Brain / Dashboard 不得接收。该栈不发布宿主机端口。
- Coolify Service 不绑定 Git push；发布时先把 `HAVEN_RELEASE_SHA` 改为已验收 commit，再手动 Restart/Redeploy。回滚时把它改回上一完整 SHA 后重新部署。

## ChatGPT OAuth（Headless 模式）

仅在 claude.ai 等平台通过 OAuth 连接时需要：

| 变量名 | 说明 |
|--------|------|
| `OMBRE_CHATGPT_OAUTH_CLIENT_ID` | OAuth Client ID |
| `OMBRE_CHATGPT_OAUTH_CLIENT_SECRET` | OAuth Client Secret |
| `OMBRE_CHATGPT_OAUTH_ACCESS_TOKEN` | 预置 Access Token |
| `OMBRE_CHATGPT_OAUTH_REFRESH_TOKEN` | 预置 Refresh Token |
| `OMBRE_CHATGPT_OAUTH_PUBLIC_BASE_URL` | OAuth 公开地址 |
| `OMBRE_CHATGPT_OAUTH_PROTECTED_HOSTS` | 受保护主机列表 |

Coolify 仅在需要向 Claude App 等公网 Remote MCP 客户端开放 Brain 时，为 Brain 注入这组变量；真实 Client Secret、Access Token 与 Refresh Token 只保存在部署平台 secret 中。Dashboard 与 Brain 位于同一 Coolify 网络时，应使用 Brain 的内部服务地址，不通过公网 OAuth 地址回连。

## Webhook

| 变量名 | 说明 |
|--------|------|
| `OMBRE_HOOK_URL` | Breath/Dream Webhook 推送地址（POST JSON），留空不推送 |
| `OMBRE_HOOK_SKIP` | 设为 `true`/`1`/`yes` 跳过 Webhook |

Webhook 推送格式（JSON）：

```json
{
  "event": "breath|dream|breath_hook|dream_hook",
  "timestamp": 1730000000.123,
  "payload": { ... }
}
```

失败仅 WARNING 日志记录，不影响主流程返回。

## 其他

| 变量名 | 说明 |
|--------|------|
| `OMBRE_AUTO_MERGE` | 覆盖 memory merge 行为（`true`/`false`） |
| `OMBRE_MEMORY_WRITE_TOKEN` | 外部写入认证令牌（留空复用 `OMBRE_GATEWAY_TOKEN`） |
| `OMBRE_DIARY_MCP_URL` | 外部日记 MCP 地址 |
| `OMBRE_DIARY_MCP_TOKEN_ENV` | 外部日记 MCP 认证 Token 所在环境变量名 |
| `OMBRE_RECALL_DIAGNOSTICS_ENABLED` | 启用召回诊断日志（默认关闭） |
| `OMBRE_RECALL_DIAGNOSTICS_PATH` | 诊断日志路径 |
| `OMBRE_RECALL_DIAGNOSTICS_MAX_CANDIDATES` | 诊断日志最大候选数 |
| `OMBRE_SCORING_WARMTH_BOOST` | 评分暖度加成（见 `bucket_manager.py`） |

## Dashboard 持久配置

非秘密热更新写入 `<state_dir>/config.runtime.yaml`；Dashboard 中新输入的 API key 写入 `<state_dir>/.env`，启动时自动读取。Coolify 已直接配置的同名环境变量优先级最高，不会被持久文件覆盖。
