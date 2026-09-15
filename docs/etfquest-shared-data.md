# ETFQuest 共享行情模式

Indicator Lab 使用 ETFQuest 的 `market_data.db` 作为行情事实源，同时保留自己的
DuckDB 保存回测历史、策略实验、指标缓存和任务状态。行情查询通过只读适配器完成；
显式的“同步全市场日线”任务通过专用 writer 写回 ETFQuest 的既有行情表，不执行
schema migration、备份或压缩。

## Docker 启动

不需要手动填写数据库路径，按宿主机系统执行对应脚本：

```bash
./scripts/start_shared.sh
```

Windows PowerShell：

```powershell
.\scripts\start_shared.ps1
```

保护版镜像在命令中增加 `--protected`：

```bash
./scripts/start_shared.sh --protected
```

脚本会自动检查：

- macOS：`~/Library/Application Support/ETFQuest/database/market_data.db`；
- Windows：`%ProgramData%\ETFQuest\database\market_data.db`；
- Linux：`$XDG_DATA_HOME/ETFQuest/database/market_data.db`，未设置时使用
  `~/.local/share/ETFQuest/database/market_data.db`。

脚本也会检查同一 ETFQuest 应用数据目录下的 `.secret.key`。Indicator Lab 只读取
ETFQuest 已保存的 v3 加密会员快照：检查同步状态、用该密钥解密，并验证服务端签发、
绑定机器码的会员签名、有效期和核心功能授权策略；不会为每个请求访问注册/会员服务器。
旧版、缺失、未同步或无法验证的快照会拒绝核心功能，需先在 ETFQuest 中联网刷新授权。

Docker 仅将 `.secret.key` 以只读方式挂载到 backend，strategy-worker 不挂载该密钥；
不要把密钥复制进仓库或镜像。手动启动 Compose overlay 时，设置
`ETFQUEST_APP_DATA_DIR` 为包含 `database/` 和 `.secret.key` 的 ETFQuest 应用数据目录。

也可以通过 `ETFQUEST_MARKET_DATABASE_DIR` 覆盖数据库目录。数据库不存在或不可读时，
脚本或授权密钥不存在/不可读时，脚本直接退出，不会启动到容器内的旧行情库或使用未经验证的会员状态。

## 全市场同步

Indicator Lab 的本地行情页提供“同步全市场日线”按钮，支持：

- 全部；
- 最近 5 年；
- 最近 3 年；
- 最近 1 年；
- 今年以来；
- 自定义开始和结束日期。

任务覆盖股票、ETF、LOF 和指数。任务会先读取 ETFQuest 已有日期覆盖，完整区间直接
跳过；最新增量或首尾缺失只请求缺失范围；如果发现中间缺口，则重新下载并覆盖整个
所选区间。网络请求由有界 worker 并发执行，临时结果写入 Indicator Lab 业务卷，
由主进程逐标的事务合并到 ETFQuest 的 `market_data` 表。

页面会显示目录发现、下载、合并阶段，当前标的、资产类型、请求/实际日期范围、完成、
跳过、失败、重试和 worker 数量。完成后会刷新行情列表、图表和行情统计，并清理内存
指标缓存；已有回测不会自动重跑，下次执行会读取最新行情。

## 同步冲突

ETFQuest 与 Indicator Lab 共用行情目录旁的 `market_data.sync.lock`。任一方执行下载
或写入前都会获取这把锁。另一个任务会收到“其他行情同步任务正在运行”的提示，返回
`409 market_data_busy`，不会继续发起网络请求。

不要在两个软件中同时点击行情下载或同步按钮。DuckDB 仍会作为最后一道写入保护，
但统一锁可以更早阻止重复网络请求和不清晰的锁冲突。

## 数据边界

共享数据库只包含 ETFQuest 既有的 `asset_info`、`market_data`、`trade_calendar` 和
`system_settings` 表。Indicator Lab 不会向共享库创建自己的业务表；业务历史、缓存、
实验任务和维护文件仍使用自己的 `/data/market` 数据卷。

发布模式要求 ETFQuest 数据库已经初始化并包含兼容 schema。缺少数据库、schema 不兼容、
权限不足或数据库正在被其它任务占用时，页面会显示可操作的错误，不会静默切换到独立
行情源。
