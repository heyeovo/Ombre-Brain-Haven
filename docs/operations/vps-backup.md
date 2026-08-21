# Ombre VPS Backblaze B2 备份运行手册

更新日期：2026-08-22

## 1. 当前状态

- Backblaze B2 账户区域：US East。
- Bucket 为 Private，默认启用 SSE-B2，Object Lock 未启用。
- restic 仓库格式：version 2，客户端压缩为 auto。
- B2 Application Key 仅限专用 Bucket，权限为 Read and Write，并允许列出 Bucket 名称。
- B2 Key、Bucket 名称和 restic 解密密码只保存在 Bitwarden；本文档不记录真值。
- VPS 端凭据目录为 `/etc/ombre-backup/`，目录权限 `0700`，其中四个文件均为 `0600 root:root`：
  - `b2-key-id`
  - `b2-application-key`
  - `repository`
  - `restic-password`
- 宿主机安装 Ubuntu 官方包：restic `0.16.4`、SQLite `3.45.1`。

## 2. 备份范围

每日备份包含：

- `/srv/ob-data/haven-test/buckets`
- `/srv/ob-data/haven-test/state`
- `/srv/ob-data/haven-test/config`
- `/srv/ob-data/claude`
- `/srv/ob-workspaces/dashboard`
- `/srv/ob-workspaces/haven`
- Coolify 数据库的 custom-format `pg_dump`
- `/data/coolify/services/5jhemgqroisbatkrbgbefueu/docker-compose.yml`
- `/data/coolify/services/5jhemgqroisbatkrbgbefueu/.env`

workspace 排除 `node_modules`、`.next`、`.turbo` 和 `coverage`。如果发现 `.env`、本地 npm/pip credential、SSH 私钥或 PEM 文件，脚本必须阻止备份并报错，不能静默上传。

Haven 当前已知共有 13 个 SQLite/DB 文件。每日脚本使用 SQLite `.backup` 逐库生成一致副本，再逐库要求 `PRAGMA integrity_check=ok`；数据库数量不是 13 时任务失败并等待人工审计。活跃 WAL/SHM 文件不会进入备份。

## 3. 脚本与 systemd

VPS 文件：

- `/usr/local/sbin/ombre-vps-backup`：`0700 root:root`
- `/usr/local/sbin/ombre-vps-prune`：`0700 root:root`
- `/etc/systemd/system/ombre-vps-backup.service`
- `/etc/systemd/system/ombre-vps-backup.timer`
- `/etc/systemd/system/ombre-vps-prune.service`
- `/etc/systemd/system/ombre-vps-prune.timer`

对应的非秘密源文件保存在仓库的 `ops/vps-backup/` 下；systemd 单元位于其 `systemd/` 子目录。

计划：

- 每日备份：`19:00 UTC`，即香港时间次日 `03:00`，最多随机延迟 5 分钟。
- 每周校验与清理：星期六 `20:00 UTC`，即香港时间星期日 `04:00`，最多随机延迟 5 分钟。
- 两个 timer 均已 `enabled`、`active`，并启用 `Persistent=true`。

保留策略仅作用于带 `ombre-vps-daily` 标签的自动快照：

- 7 个日备份
- 4 个周备份
- 3 个月备份

每周清理必须先完成 `restic check --read-data`；检查失败时，`forget --prune` 不会执行。人工里程碑快照没有 `ombre-vps-daily` 标签，因此不会被自动删除。

## 4. 日常检查

查看 timer：

```bash
systemctl list-timers --all 'ombre-vps-*' --no-pager
```

查看最近一次备份结果：

```bash
systemctl show ombre-vps-backup.service \
  -p Result -p ExecMainStatus -p ActiveState -p SubState
journalctl -u ombre-vps-backup.service -n 50 --no-pager
```

成功信号：

- `Result=success`
- `ExecMainStatus=0`
- 日志包含 `OMBRE_VPS_BACKUP_OK`

`Type=oneshot` 服务完成后显示 `inactive/dead` 是正常状态；timer 应继续为 `active`。

手动触发一次备份：

```bash
systemctl start ombre-vps-backup.service
```

手动运行清理脚本会真实删除超出策略的自动快照，执行前必须重新确认范围：

```bash
systemctl start ombre-vps-prune.service
```

## 5. 恢复原则

- 永远先恢复到 `/srv/ob-backups/haven/restore-test-*` 等隔离目录。
- 不允许直接把 restic snapshot 覆盖到 `/srv/ob-data`、workspace 或 `/data/coolify`。
- 恢复后先做全文件 SHA 比较、13 个 SQLite integrity 检查和 Coolify `pg_restore --list`。
- 真正回灌正式数据必须另开恢复窗口，先停止对应写入并准备 rollback。
- 丢失 restic 密码将导致 B2 数据不可恢复；密码只从 Bitwarden 取用。

列出快照时，在 root shell 中临时加载凭据，操作后立即 unset；不得把值放进命令参数、聊天或日志：

```bash
set +x
export B2_ACCOUNT_ID="$(< /etc/ombre-backup/b2-key-id)"
export B2_ACCOUNT_KEY="$(< /etc/ombre-backup/b2-application-key)"
export RESTIC_REPOSITORY_FILE=/etc/ombre-backup/repository
export RESTIC_PASSWORD_FILE=/etc/ombre-backup/restic-password
export RESTIC_CACHE_DIR=/srv/ob-backups/haven/.restic-cache

restic snapshots

unset B2_ACCOUNT_ID B2_ACCOUNT_KEY RESTIC_REPOSITORY_FILE RESTIC_PASSWORD_FILE RESTIC_CACHE_DIR
```

## 6. 2026-08-22 首次验收

- 完整人工里程碑快照：`d70eaaf7`。
- 首个自动标签试跑快照：`a81c6087`。
- systemd 启动验收快照：`0a7aca4a`。
- 最终候选快照恢复出 929 个文件，原始与恢复 SHA 全部一致。
- 13 个 Haven SQLite/DB 均恢复且 `integrity_check=ok`。
- Coolify custom-format dump 可被 `pg_restore --list` 读取，共 579 个归档条目。
- 恢复后的 Coolify `.env` 与数据库 dump 均为 `0600 root:root`。
- `restic check --read-data` 无错误。
- retention dry-run 只命中 `ombre-vps-daily` 快照，人工快照不受影响。
- 三个本地明文 staging/restore 目录验收后已精确删除；B2 快照保留。

## 7. 尚未完成

- 尚未在本机保存额外的加密恢复副本；B2 是当前主要异地副本，Bitwarden 保存恢复凭据。
- 尚未配置备份失败的外部通知；失败只能通过 systemd 状态和 journal 发现。
- Coolify dump 已完成归档结构验证，但没有向独立 PostgreSQL 实例做完整回灌；这不影响 Haven 数据的完整恢复验收，后续可在专门灾难恢复窗口补做。
- 实际自动清理尚未发生；目前只有 retention dry-run。第一次周日清理后应复核 journal 和剩余 snapshot。
