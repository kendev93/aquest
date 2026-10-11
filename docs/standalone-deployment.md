# 独立分发部署（Docker）

面向最终用户的部署方式：用户拉镜像、起容器，容器自己同步行情，不需要本机装过任何外部行情软件。

项目只有这一种形态。历史上曾经支持的"读取外部共享行情库"模式已随 4 个模块一并移除，
数据边界因此变得很窄：**本机 DuckDB 是唯一的行情事实源**。

## 快速开始

```bash
export INDICATOR_LAB_IMAGE=<你的镜像标签>
sh scripts/start.sh
```

`scripts/start.sh` 是日常入口，默认带 `--build`——改完代码直接跑它就会重建镜像：

| 命令 | 作用 |
|---|---|
| `sh scripts/start.sh` | 重建镜像并启动 |
| `sh scripts/start.sh --build-only` | 只构建，不启动 |
| `sh scripts/start.sh --no-build` | 不重建，直接启动 |

它会先跑 `docker compose config --quiet` 预校验再动手；`DATA_DIR` 无论来自环境变量还是
`.env` 都会被识别，并自动叠加绑定挂载覆盖文件。

不用脚本也行，等价命令是 `docker compose up --build -d`。

> ⚠️ 直接敲 `docker compose up -d` **不会**重建已有镜像。代码改了却忘记 `--build`，
> 容器会静默继续跑旧镜像——这是从旧启动脚本迁过来最容易踩的坑。

打开 `http://127.0.0.1:${WEB_PORT:-5173}`。首次进入后触发一次全市场同步，行情会存进数据卷。

## 数据放在哪里

默认是一个 Docker 命名卷 `indicator-lab_indicator-lab-data`，挂到容器的 `/data/market`。

**为什么默认不用绑定挂载**：绑定挂载要求宿主目录允许容器内的 `appuser`(uid 10001) 写入。
在 Docker Desktop（macOS/Windows）上 uid 映射宽松所以没事，但换到 Linux、群晖、NAS
就会出现 `PermissionError`——那正是做 Docker 分发想躲开的问题。命名卷由 Docker 管理属主，
不存在这一层。

想知道数据实际落在宿主机的哪个路径：

```bash
docker volume inspect indicator-lab_indicator-lab-data
```

## 备份与恢复

行情和指标缓存可以从数据源重新同步/重算，**用户的研究成果不能**。所以备份只导出后者：
回测历史、条件研究、策略发现、选股历史。

一份完整安装里，这些资产大约 **13 MB**，而数据库本身约 **4 GB**——比例约 1:300。
把两者放在同一个文件里、靠整库拷贝来保护资产，代价过高。

### 网页界面

`GET /api/v1/data-transfer/export` 下载一个 zip，`POST /api/v1/data-transfer/import` 上传合并。

### 命令行

```bash
# 导出（默认写到当前目录，或用 -o 指定文件/目录）
docker compose -f docker-compose.yml exec backend \
  python -m selector_app export -o /data/market

# 导出的文件在数据卷里，拷出来：
docker compose -f docker-compose.yml cp \
  backend:/data/market/indicator-lab-backup-<时间戳>.zip .

# 查看备份里有什么
python -m selector_app manifest indicator-lab-backup-<时间戳>.zip

# 导入（合并，已存在的记录跳过，不会覆盖或删除任何东西）
docker compose -f docker-compose.yml cp \
  indicator-lab-backup-<时间戳>.zip backend:/tmp/
docker compose -f docker-compose.yml exec backend \
  python -m selector_app import /tmp/indicator-lab-backup-<时间戳>.zip
```

导入是**幂等合并**：同一份备份导入两次，第二次全部记为 skipped。

## 磁盘占用

数据库会随行情和缓存增长（全市场日线约 4 GB 量级）。默认配置**不保留整库备份**：
`SELECTOR_DB_BACKUP_RETENTION_DAYS=0` 表示压缩期间临时留一份用于回滚，成功后立即删除。

要保留几天就设成天数，但注意**每份备份都和数据卷一样大**：

```bash
SELECTOR_DB_BACKUP_RETENTION_DAYS=3 docker compose -f docker-compose.yml up -d
```

压缩用的是硬链接而不是整库拷贝，所以压缩期间的峰值是两份库大小而不是三份。

## 换成自己管理的目录

想让数据落在一个能直接看到、直接备份的宿主目录，叠加覆盖文件：

```bash
sudo mkdir -p /srv/indicator-lab
sudo chown 10001:10001 /srv/indicator-lab     # ← 必须，否则容器起不来

DATA_DIR=/srv/indicator-lab docker compose \
  -f docker-compose.yml \
  -f docker-compose.bind.yml up -d
```

覆盖文件只改变卷的落盘位置，服务定义不动。**属主必须自己改**：绑定挂载时 Docker 不会
把镜像里的目录属主带过来。

## 升级

```bash
docker compose -f docker-compose.yml pull
docker compose -f docker-compose.yml up -d
```

命名卷在容器重建时保持不变，行情和研究记录都会留着。

> ⚠️ `docker compose down -v`、`docker volume prune`、以及 Docker Desktop 的
> Reset 都会**静默删除命名卷**。在跑这些命令之前先导出一份备份。

## 相关环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `INDICATOR_LAB_IMAGE` | 必填 | 后端与 worker 的镜像标签 |
| `WEB_PORT` | `5173` | 前端端口（绑定 127.0.0.1） |
| `API_PORT` | `8123` | 后端端口（绑定 127.0.0.1） |
| `SELECTOR_DB_BACKUP_RETENTION_DAYS` | `0` | 整库备份保留天数，0 表示不保留 |
| `SELECTOR_AUTO_SYNC_ENABLED` | `1` | 启动后自动同步在线行情 |
| `SELECTOR_DISCOVERY_WORKERS` | `1` | 默认仅 1 个评估进程，降低 CPU 压力；设置 `auto` 可按可见 CPU 和内存分配 |
| `SELECTOR_STRATEGY_POOL_MAX_WORKERS` | `1` | 默认同时研究 1 只；设置正整数或 `auto` 可增加股票并发，评估进程仍受共享预算限制 |
| `SELECTOR_DUCKDB_THREADS` | `1` | DuckDB 查询线程数，仅注入策略 worker；研究查询都是单标的小查询，默认 1 把 CPU 让给股票并发，设 `0` 恢复 DuckDB 默认 |
| `OPENBLAS_NUM_THREADS` 等 | `1` | BLAS/OpenMP 线程池上限（另有 `OMP_NUM_THREADS`/`MKL_NUM_THREADS`/`NUMEXPR_MAX_THREADS`）；worker 进程内已默认压到 1，个别负载想放开时显式覆盖 |
| `SELECTOR_DISCOVERY_MEMORY_BUDGET_MB` | `auto` | 内存预算 |
| `STRATEGY_WORKER_MEMORY_LIMIT` | 无 | 容器内存上限，如 `6g` |
| `DATA_DIR` | 无 | 仅在叠加绑定挂载覆盖文件时需要 |

`SELECTOR_DUCKDB_THREADS` 与 BLAS 四变量默认把策略 worker 的「隐形并行」（DuckDB
查询线程与 BLAS 线程池）压到单线程：单个 worker 不再占满整机 CPU。想让全市场
研究提速，应在此基础上调大 `SELECTOR_STRATEGY_POOL_MAX_WORKERS` 与
`SELECTOR_DISCOVERY_WORKERS`——先压隐形并行、再放真实并发，顺序不能反。


### 策略池详情与跟踪升级

本次升级新增 `pool_tracking_versions` 表，不改写 OHLC 行情，不改变原策略追踪的空仓上线方式。跟踪账户的曲线、成交和检查点压缩保存；详情分页读取，列表只返回摘要。

已有策略在首次初始化时记录实际上线时间，不沿用历史 `updated_at`。普通重跑相同规则不重置版本；清空重建会归档旧跟踪轮次。承接创建前持仓时，创建至今收益从原买入执行前的账户净值计算，上线后收益单独展示。

回放语义升级会在下一次计划或手动跟踪时重建旧账户检查点；启动时保留策略版本、创建时间和实际上线日期，不会立即重放账户历史。

部署需同步更新后端、前端和策略 worker。新增跟踪消费通道与寻优共享计算预算；旧 worker 不支持新操作。后台自动跟踪默认开启，可用 `SELECTOR_POOL_TRACKING_ENABLED=0` 暂停自动提交，手动刷新仍可使用。迁移为新增表，升级前可按原有流程备份数据卷，无须自动复制整个行情库。

## 行情数据源与可选依赖

行情按用途分两层取源（选型实测见 `docs/archive/datasource-survey-2026-09-20.md`）：

- **日线**：腾讯日 K 为主源，东方财富作为容错备用。两者都免费、无需密钥。
- **证券目录**：baostock 的 `query_all_stock`（可选依赖，见下），东方财富目录接口兜底。

同步会先检查本地覆盖范围，自动跳过完整区间并补齐缺失区间。在线通道覆盖沪深主板、科创板、创业板。

### 证券目录依赖（`catalog`）

东方财富的 `clist` 目录接口已被封禁，全市场标的发现现在主要靠 baostock。它放在可选依赖 `catalog` 里：

```bash
python -m pip install -e ".[dev,catalog]"
```

没装也能正常启动，只是目录会退回东方财富——**而那个接口目前是封禁状态，所以表现是「同步成功但拿不到新标的」**，日志里会有明确告警。Docker 镜像已默认安装。

## 导入通达信本地日线（可选）

容器自带行情同步能力，不依赖任何外部行情软件；这一节只在你想用通达信已下载的日线做覆盖或补全时才需要。

在通达信里执行「盘后数据下载」下载日线，然后把 vipdoc 目录挂进容器：

```bash
TDX_VIPDOC_PATH=/你的/通达信/vipdoc docker compose \
  -f docker-compose.yml -f docker-compose.tdx.yml up -d
docker compose build backend && docker compose \
  -f docker-compose.yml -f docker-compose.tdx.yml up -d
```

也可以只挂上级目录 `TDX_HOST_ROOT=...`（挂到容器内 `/host`），之后在页面上填 `/host` 下的任意子路径即可。后端跑在容器里，只能看到已挂载的路径，直接填电脑上的原始目录是找不到的。

之后在「本地行情」页（`/market-data`）点「自动检测」，找到的目录会自动填入；导入按同一只股票同一天覆盖库里已有的行情，目录里没有的日期保持原样。

## 清理历史库里的非股票数据

代码层面已经不再接受 ETF/基金、指数、债券和 B 股，但**升级前同步进来的旧数据还留在库里**。用下面这个脚本清掉（默认只体检，不会改数据；必须先停 backend，DuckDB 是单写者）：

```bash
docker compose stop backend
scripts/container_python.sh scripts/prune_non_stock_data.py            # 先看会删什么
scripts/container_python.sh scripts/prune_non_stock_data.py --apply --yes
docker compose start backend
```

删除前会在库文件旁边写一份 `market.duckdb.prune-backup-<时间戳>`，确认无误后自己删掉备份即可（脚本不会自动清理）。指标缓存是可重建的派生数据，会连同被删标的的缓存行一起清掉。
