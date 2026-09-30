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
| `sh scripts/start.sh --protected` | 用 Nuitka 编译版镜像 |

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
| `SELECTOR_DISCOVERY_WORKERS` | `auto` | 策略发现并发度 |
| `SELECTOR_DISCOVERY_MEMORY_BUDGET_MB` | `auto` | 内存预算 |
| `STRATEGY_WORKER_MEMORY_LIMIT` | 无 | 容器内存上限，如 `6g` |
| `DATA_DIR` | 无 | 仅在叠加绑定挂载覆盖文件时需要 |
