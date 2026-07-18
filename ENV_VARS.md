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
| `OMBRE_GATEWAY_ADMIN_URL` | — | Gateway 管理 API 地址 |
| `OMBRE_GATEWAY_DEBUG_TIMEOUT_SECONDS` | — | Gateway debug 请求超时（默认 30s） |
| `OMBRE_DOMAIN_SENTINEL_MODEL` | `gateway.domain_sentinel_model` | 领域哨兵模型 |
| `OMBRE_DOMAIN_SENTINEL_API_KEY` | — | 领域哨兵 API Key |

> `OMBRE_GATEWAY_TOKEN` 用于客户端 Bearer 认证和 Gateway Debug 端点访问。各 upstream 的 API key 通过 `gateway.upstreams[*].api_key_env` 分别指向独立环境变量。

## Persona / Reflection / Portrait / Dream

`config.yaml` 字段：`persona.*` / `reflection.*` / `portrait.*` / `dream.*`

| 变量名 | 覆盖的 YAML 键 | 说明 |
|--------|----------------|------|
| `OMBRE_PERSONA_API_KEY` | `persona.api_key` | Persona 模型密钥 |
| `OMBRE_PERSONA_BASE_URL` | `persona.base_url` | Persona 模型地址 |
| `OMBRE_PERSONA_MODEL` | `persona.model` | Persona 模型名 |
| `OMBRE_REFLECTION_API_KEY` | `reflection.api_key` | Reflection 模型密钥 |
| `OMBRE_REFLECTION_BASE_URL` | `reflection.base_url` | Reflection 模型地址 |
| `OMBRE_REFLECTION_MODEL` | `reflection.model` | Reflection 模型名 |
| `OMBRE_REFLECTION_CANDIDATE_MODEL` | `reflection.daily_chat_memory_candidate_model` | 自动记忆候选模型 |
| `OMBRE_DREAM_API_KEY` | `dream.api_key` | Dream 模型密钥 |
| `OMBRE_DREAM_BASE_URL` | `dream.base_url` | Dream 模型地址 |
| `OMBRE_DREAM_MODEL` | `dream.model` | Dream 模型名 |
| `OMBRE_DREAM_ENABLED` | `dream.enabled` | 启用/关闭夜梦 |

## 存储路径

| 变量名 | 说明 |
|--------|------|
| `OMBRE_BUCKETS_DIR` | 记忆桶文件存放目录（默认 `./buckets`；Docker Volume 挂载时务必设置） |
| `OMBRE_STATE_DIR` | 运行状态目录（默认 `<buckets_dir>/../state`），含 embedding DB、portrait 等 |
| `OMBRE_RUNTIME_CONFIG_PATH` | 运行时配置路径（默认 `<state_dir>/config.runtime.yaml`） |

## 传输与网络

| 变量名 | 说明 |
|--------|------|
| `OMBRE_TRANSPORT` | MCP 传输模式：`stdio` / `sse` / `streamable-http`（默认 `stdio`） |
| `OMBRE_PORT` | HTTP/SSE 模式监听端口（默认 `8000`） |

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

## Zeabur 部署建议

不必逐个设 env var。只设核心 4 个 + 挂载 `config.yaml` 持久化 Volume。运行时通过 Dashboard 热更新配置，回写到 `config.yaml`（挂载不可写时 fallback 到 `state/config.runtime.yaml`）。
