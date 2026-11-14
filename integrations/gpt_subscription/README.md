# GPT subscription 管理工具

本目录只负责授权和文字传输，不是聊天 runner。API、密文布局、错误和并发边界以 [Haven reference](../../docs/reference.md#GPT-订阅授权与传输gateway) 为准；密钥变量只在 [ENV_VARS](../../ENV_VARS.md#GPT-订阅聊天gateway可选) 维护。官方源码版本和许可证见 [vendor/UPSTREAM.md](vendor/UPSTREAM.md)。

## 准备与测试

正式依赖来自本目录 lockfile，不引用相邻独立验证目录：

```sh
npm ci --ignore-scripts --no-audit --no-fund
npm test
```

需要 Node 22.12+；npm test 编译 vendor TypeScript，再运行官方回归和 Haven 合成凭据测试。Docker 构建阶段同样运行这些测试，最终镜像保留运行依赖、许可证及管理命令，不需要运行时 npm 安装。测试不会访问真实账号。Windows 上原有的 Linux 文件权限/符号链接测试会按上游条件跳过，完整验证须在 Linux 运行。

## 服务器准备（先确认实际配置）

此处是操作方案，不是当前线上配置；执行前必须人工确认 Gateway 的容器名、buckets_dir 持久挂载、运行 UID、当前 `profile_id` 和 secret 挂载。只部署已验收分支，不能以本文示例直接改正式服务。

1. 在数据卷外的独立 secret 存储中生成一个 base64 编码的随机 32 字节密钥，文件只读或仅所有者可写，不在命令输出中显示。不要覆盖已有密钥。密钥与数据分别备份，丢失密钥不能恢复原密文。
2. 将该普通文件只读挂载到 Gateway，按 ENV_VARS 指向其绝对路径。保证运行账号有读取权限、私有授权目录有写入权限。原授权数据库/文件和密钥均不提交代码。
3. 本机准备已验收版本及 Node 依赖。为此次 Haven 交接使用这个专用工具的新授权，不读取旧验证目录的 DPAPI 文件。Windows 本机恢复记录采用当前用户 DPAPI；换 Windows 用户不能解密。不要同步 `.local-auth`。
4. 确保 SSH alias 已固定主机密钥且可免交互登录。准备远端命令，进入**实际 Gateway 容器**执行本目录的 `admin.mjs`。命令参数包含可信 Haven profile 和实际 buckets 目录，不含任何令牌或密钥值。

管理入口命令模板（其中值必须替换为人工确认的真实值）：

```text
docker exec -i <实际Gateway容器名> node /app/integrations/gpt_subscription/admin.mjs --buckets-dir <实际buckets目录> --profile <可信profile_id>
```

服务内与管理命令必须使用相同的持久目录和运行身份。不要用 `docker exec -t`，不要把环境导出、token JSON、登录 URL 或 SSH 管道内容保存到日志。

## 本机登录并交接

在本目录打开终端，使用上一步已核对的完整远端命令：

```text
node login-transfer.mjs --host <SSH别名> --remote-command "<上一步完整命令>"
```

工具先取得目标的 host ID 和导入 generation，再打开本机浏览器授权；回调只监听 127.0.0.1。首次不强制重新同意；只有用户明确选择重新开启订阅使用权限时，才加 `--reconsent`。工具不打印授权 URL、身份或令牌。

每组 SSH 别名＋完整远端命令有独立本机加密记录，保存在 `.local-auth/<hash>/`。成功登录后，工具在发起 SSH 前先将待交接授权加密保存，并移除本机活动凭据；本机副本不能发模型请求或刷新。access/refresh/client ID/保留的 ID token 只经 SSH stdin 传给 Node 管理命令，在服务器使用独立密钥重新加密，不复制 Windows 密文。

服务器核验成功后返回仅含交接编号的回执，工具再删除本机待交接令牌。若连接中断、回执丢失或暂时故障，**保持相同 SSH 参数**加 `--retry`；服务器回放同一回执，不覆盖后来轮换的令牌。若交接过期或 generation 冲突，重新执行不带 --retry 的命令，以同一本机登记重新登录。重新登录仍复用已颁发 client ID 和目标 host ID，不让本机成为第二个刷新端。

本机工具保留加密的登记、身份验证提示和 pending registration，便于取消登录、一次性 code 过期及重新授权后继续恢复。不要用其他程序读取 `.local-auth` 并刷新里面待交接的令牌。若本机记录丢失，可新建登记导入；旧登记在界面明确选择/断开，不能按 email 混并。

登录后用已认证的 connection/models 接口检查掩码状态和模型列表。没有设置密钥、暂时网络失败、缺少共享权限或 import 失败，都不能视为已连接成功。

## 验收层级

合成测试证明协议、加密、并发、错误和恢复逻辑；它不能证明真实 provider 的轮换、撤销或正式 Linux 部署已通过。真实账号导入、密钥 secret 设置、VPS 服务修改和刷新观察必须在获授权的联调步骤单独执行。准确进度只维护在 Dashboard 对应 handoff，不在本文登记阶段状态。
