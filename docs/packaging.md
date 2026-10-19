# 从源码构建与运行

本文面向想从源码运行 AQuest 的用户与贡献者。日常使用推荐 Docker Compose（见 README 的"快速开始"与 [独立分发部署](standalone-deployment.md)）；不想用容器时，按本文在宿主机直接运行 Python 后端与前端。

## 准备运行环境

项目根目录执行（Python 3.14；仓库里的锁目前仍是在 3.12 下编译的，重编译要在 3.14 环境里做）：

~~~bash
python3 -m pip install --require-hashes -r requirements.txt
python3 -m pip install -e ".[dev,catalog]"
~~~

运行依赖由 Python 3.14 下 `requirements.in` 编译成 `requirements.txt`，包含精确版本和 SHA-256（在库的那份仍是在 3.12 下编译的：2026-10-19 实测它在 `python:3.14-slim` 下能按哈希原样装上、挑到的都是 cp314 轮子，但改直接依赖范围时要按 3.14 重新编译）；Docker 与 CI 使用 `pip install --require-hashes -r requirements.txt`。修改直接依赖范围后，使用开发环境中的 pip-tools 运行 `python -m piptools compile --generate-hashes --strip-extras --resolver=backtracking --output-file=requirements.txt requirements.in` 并提交两个文件。Dependabot 每周为 pip-tools、npm、Docker 基础镜像和 GitHub Actions 检查升级并创建 PR。

## 统一入口

`pip install -e .` 会注册统一的 `aquest` 命令（定义见 `pyproject.toml` 的 `[project.scripts]`），按子命令启动：

~~~text
aquest server              启动 FastAPI backend
aquest worker              启动 strategy-worker
aquest export [-o 目标]    导出用户资产备份
aquest import <zip>        合并导入用户资产备份
aquest manifest <zip>      查看备份内容，不写入
~~~

不带子命令时等同于 `server`，旧的 `--host/--port` 调用方式继续可用。开发时也可以直接用 uvicorn 起热重载服务：

~~~bash
uvicorn aquest.web.app:app --reload --host 127.0.0.1 --port 8123
~~~

## 原生运行的数据与备份

原生（非 Docker）运行时行情数据库默认落在用户数据目录 `~/.aquest/market.duckdb`，可以用 `AQUEST_DATA_DIR` 改到别处；数据库由后端自己从在线数据源同步，不需要外部行情软件的任何文件或目录。

备份用户自己的研究成果用子命令：

~~~bash
aquest export -o /path/to/backups     # 目标目录存在时写入 aquest-backup-<时间戳>.zip
aquest manifest <zip>                 # 只查看归档内容，不写入
aquest import <zip>                   # 合并导入，已存在的记录跳过
~~~

归档只包含回测、选股、策略发现和复核记录这四类用户资产，行情和指标缓存不在其中——它们可以重新同步或重算。原生部署同样可以跑独立的 strategy-worker：backend 用 `AQUEST_STRATEGY_WORKER_TOKEN_FILE` 指定 token 文件并用 `AQUEST_STRATEGY_WORKER_TOKEN_AUTOGENERATE=1` 允许首次启动自动生成，worker 端设置同一个 `AQUEST_STRATEGY_WORKER_TOKEN_FILE`，并用 `AQUEST_STRATEGY_WORKER_API_URL` 指向 backend（默认值 `http://backend:8123` 只适用于 Compose 网络）。

## 运行前端

~~~bash
cd web-ui
npm ci
npm run build
~~~

前端由 `Dockerfile` 的 node 构建阶段编出 `web-ui/dist`，运行时容器内的 nginx 提供静态文件并把 `/api` 代理到同容器的 uvicorn(8123)；本地开发直接 `npm run dev`，走 Vite 的代理配置即可。

## 发布前检查

- [ ] 运行 `sh scripts/verify_ci.sh python`；
- [ ] backend `/healthz` 正常；
- [ ] strategy-worker 可以领取并完成一个受控测试任务；
- [ ] 使用一次受控的在线同步验证行情写入，再跑通回测和策略实验；
- [ ] 用 export/import 往返验证用户资产备份（行情和指标缓存不应出现在归档里）；
- [ ] 确认首次启动自动生成了 worker token，且容器重建后数据卷内容仍在。

## 相关文件

- [Dockerfile](../Dockerfile)
- [docker/nginx.conf](../docker/nginx.conf)
- [docker/supervisord.conf](../docker/supervisord.conf)
- [docker-compose.yml](../docker-compose.yml)
- [docker-compose.bind.yml](../docker-compose.bind.yml)
- [独立分发部署](standalone-deployment.md)
