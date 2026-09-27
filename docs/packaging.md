# Indicator Lab 打包与发布指南

本文面向需要构建并分发 Indicator Lab 的维护者。目标是让用户拿到可运行产物，而不是项目源代码。

当前仓库提供的是 Nuitka standalone 文件夹产物。Nuitka 官方建议先验证 standalone，再考虑 onefile；本项目依赖 NumPy、Pandas、LightGBM，在线行情由内置 HTTP 客户端直接访问，默认保留文件夹模式以减少启动解包和排错成本。

## 先选发布方式

### 推荐：保护版 Docker 镜像

适合希望 Windows 和 macOS 用户使用同一套运行方式的场景。构建时使用源码，最终镜像只复制编译后的 bundle：

~~~text
Dockerfile.protected
  builder：安装依赖、编译 Nuitka、扫描产物
  runtime：只保留 indicator-lab.dist 和运行库
~~~

### 原生 standalone 文件夹

适合需要给用户一个 Windows 或 macOS 压缩包的场景。必须在目标平台和目标架构上构建：

- Windows x64 产物不能在 macOS 上直接构建出来；
- macOS arm64 和 x86_64 必须分别构建；
- 当前原生产物是 backend/worker 服务 bundle，不是双击即用的桌面 .exe 或 .app；
- 前端仍需要由 Nginx、Node.js 或其他 HTTP 服务提供。

如果要求更强的源码保护，代码必须留在你的服务器上，由客户端调用远程服务。本地可执行程序只能提高逆向门槛。

## 共同准备

项目根目录执行：

~~~bash
python3 -m pip install --require-hashes -r requirements.lock
python3 -m pip install -r requirements-build.txt
~~~

requirements-build.txt 固定 Nuitka、ordered-set 和 zstandard 的构建版本。保护版 Docker 会把这些依赖留在 builder 阶段，不复制到最终运行镜像。

运行依赖由 Python 3.12 下 `requirements.in` 编译成 `requirements.lock`，包含精确版本和 SHA-256；Docker 与 CI 使用 `pip install --require-hashes -r requirements.lock`。修改直接依赖范围后，使用开发环境中的 pip-tools 运行 `python -m piptools compile --generate-hashes --strip-extras --resolver=backtracking --output-file=requirements.lock requirements.in` 并提交两个文件。

通用构建脚本为：

~~~text
scripts/build_nuitka.py
scripts/nuitka_entrypoint.py
scripts/verify_nuitka_bundle.py
~~~

统一入口按子命令启动：

~~~text
indicator-lab server              启动 FastAPI backend
indicator-lab worker              启动 strategy-worker
indicator-lab export [-o 目标]    导出用户资产备份
indicator-lab import <zip>        合并导入用户资产备份
indicator-lab manifest <zip>      查看备份内容，不写入
~~~

不带子命令时等同于 `server`，旧的 `--host/--port` 调用方式继续可用。

构建参数可以通过命令行或环境变量设置：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| --output-dir | dist/protected | 产物目录 |
| --cache-dir | dist/.nuitka-cache | Nuitka 编译缓存 |
| --jobs / NUITKA_JOBS | 4 | 编译并发，笔记本建议 2 |
| --lto / NUITKA_LTO | auto | LTO 模式 |
| --reproducible / NUITKA_REPRODUCIBLE | auto | 可复现构建模式 |
| --clean | 关闭 | 只清理当前保护版目录中的已知产物 |

## 保护版 Docker 发布

保护版直接复用默认编排，只替换构建方式和启动入口。运行下面的 Compose 命令前，先设置
`INDICATOR_LAB_IMAGE` 为本次发布的镜像标签；数据落在默认命名卷里，不需要任何外部行情软件
的目录，也不需要额外的密钥文件。后端首次启动会在数据目录生成
`/data/market/.strategy-worker-token`，backend 与 strategy-worker 共用这一个 token
（由 `docker-compose.yml` 中的 `SELECTOR_STRATEGY_WORKER_TOKEN_AUTOGENERATE=1` 打开，
worker 只读取它）。

### 构建

~~~bash
export INDICATOR_LAB_IMAGE=indicator-lab-protected:1.0.0
docker compose \
  -f docker-compose.yml \
  -f docker-compose.protected.yml \
  build
~~~

笔记本上建议降低构建并发：

~~~bash
NUITKA_JOBS=2 docker compose \
  -f docker-compose.yml \
  -f docker-compose.protected.yml \
  build
~~~

### 启动

构建出的镜像用同一组 Compose 文件启动；命名卷 `indicator-lab-data` 会在首次启动时创建，
数据落在 `/data/market`，容器重建不会丢：

~~~bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.protected.yml \
  up -d
~~~

打开 `http://127.0.0.1:${WEB_PORT:-5173}`，后端在 `127.0.0.1:${API_PORT:-8123}`。

这套 Compose 把命名卷 `indicator-lab-data` 挂到容器 `/data/market`，backend 与 strategy-worker 共用它：backend 是 DuckDB 的唯一写者，负责在线同步和任务状态写入；strategy-worker 只读打开同一个数据库，并通过 backend 的容器内认证接口回传领取、进度和结果。默认不是绑定挂载；想让数据落在自己管理的宿主目录（便于直接备份或迁移），叠加 `docker-compose.bind.yml` 并把 `DATA_DIR` 指向该目录，此时宿主目录必须允许容器内 `appuser`（uid 10001）写入：

~~~bash
DATA_DIR=/srv/indicator-lab docker compose \
  -f docker-compose.yml \
  -f docker-compose.bind.yml \
  -f docker-compose.protected.yml \
  up -d
~~~

绑定挂载时 Docker 不会把镜像里的目录属主带过来，必须先 `sudo mkdir -p "$DATA_DIR" && sudo chown 10001:10001 "$DATA_DIR"`；群晖/NAS 或 macOS/Windows 上的共享目录往往改不了属主，那种情况直接用默认命名卷。发布验证不要在用户的真实数据上做，换成临时命名卷或临时 `DATA_DIR`。

### 发布前检查

~~~bash
sh scripts/verify_ci.sh protected
~~~

Dockerfile.protected 的 builder 阶段会执行 verify_nuitka_bundle.py。如果 standalone 目录内出现 .py、.pyc 或 .pyo，构建会失败。

发布给用户时只导出最终镜像或镜像仓库中的最终 tag，不要导出 builder stage、构建上下文、Dockerfile 或 Nuitka 缓存。

## Windows 原生打包

### 前置条件

- Windows x64；
- Python 3.12 x64；
- 可用的 MSVC 或 Nuitka 可使用的 MinGW 编译环境；
- 有足够的磁盘空间和内存，首次编译会生成较大的 C/C++ 构建中间文件。

### 创建打包环境

在 PowerShell 中：

~~~powershell
py -3.12 -m venv .venv-pack
.\.venv-pack\Scripts\Activate.ps1

python -m pip install --upgrade pip
python -m pip install --require-hashes -r requirements.lock
python -m pip install -r requirements-build.txt
~~~

### 构建 standalone bundle

~~~powershell
$env:NUITKA_JOBS = "2"
$env:NUITKA_LTO = "auto"
$env:NUITKA_REPRODUCIBLE = "yes"
python scripts\build_nuitka.py --output-dir dist\protected-windows --clean
~~~

产物通常位于：

~~~text
dist\protected-windows\indicator-lab.dist\indicator-lab.exe
~~~

检查源码和字节码：

~~~powershell
python scripts\verify_nuitka_bundle.py dist\protected-windows\indicator-lab.dist
~~~

本地启动 backend：

~~~powershell
.\dist\protected-windows\indicator-lab.dist\indicator-lab.exe server --host 127.0.0.1 --port 8123
~~~

strategy-worker 使用同一个 bundle：

~~~powershell
.\dist\protected-windows\indicator-lab.dist\indicator-lab.exe worker
~~~

压缩分发目录：

~~~powershell
Compress-Archive -Path dist\protected-windows\indicator-lab.dist -DestinationPath dist\indicator-lab-windows-x64.zip
~~~

当前通用脚本还没有接入 Windows 安装器、代码签名和无黑窗选项。正式提供安装包前，需要补充 --windows-console-mode=disable、VC++ 运行库策略、签名和安装器测试。

## macOS 原生打包

### 前置条件

- Apple Silicon 使用 arm64 Python；Intel Mac 使用 x86_64 Python；
- Xcode Command Line Tools；
- Homebrew；
- libomp，因为当前 LightGBM macOS 动态库依赖它。

### Apple Silicon arm64

~~~bash
python3 -m venv .venv-pack
source .venv-pack/bin/activate

python -m pip install --upgrade pip
python -m pip install --require-hashes -r requirements.lock
python -m pip install -r requirements-build.txt
brew install libomp

export NUITKA_JOBS=2
export NUITKA_LTO=auto
export NUITKA_REPRODUCIBLE=yes
python scripts/build_nuitka.py --output-dir dist/protected-macos-arm64 --clean
~~~

### Intel x86_64

必须用 x86_64 Python 创建虚拟环境，并用同一个解释器安装依赖和编译：

~~~bash
arch -x86_64 python3 -m venv .venv-pack-x86_64
source .venv-pack-x86_64/bin/activate

arch -x86_64 python -m pip install --require-hashes -r requirements.lock
arch -x86_64 python -m pip install -r requirements-build.txt
arch -x86_64 python scripts/build_nuitka.py --output-dir dist/protected-macos-x86_64 --clean
~~~

如果机器上没有可运行的 x86_64 Python，需要先安装对应架构的 Python，再执行上面的命令。

### 补齐 LightGBM 的 OpenMP 动态库

当前构建脚本会定位并复制 lib_lightgbm.dylib，但 macOS LightGBM wheel 还依赖 libomp.dylib。编译完成后，对应 bundle 执行：

~~~bash
bundle="dist/protected-macos-arm64/indicator-lab.dist"
lightgbm="$bundle/lightgbm/lib/lib_lightgbm.dylib"
cp "$(brew --prefix libomp)/lib/libomp.dylib" "$bundle/lightgbm/lib/libomp.dylib"
install_name_tool -change '@rpath/libomp.dylib' '@loader_path/libomp.dylib' "$lightgbm"
python scripts/verify_nuitka_bundle.py "$bundle"
~~~

Intel 版本将 bundle 改成 dist/protected-macos-x86_64/indicator-lab.dist。可以用下面的命令确认动态库依赖：

~~~bash
otool -L "$lightgbm"
~~~

压缩分发目录：

~~~bash
ditto -c -k --sequesterRsrc --keepParent "$bundle" dist/indicator-lab-macos-arm64.zip
~~~

当前仓库还没有自动化 macOS .app、Developer ID 签名和 notarization。给外部用户发布前，需要在 macOS 构建机补齐这些步骤；本地 standalone 文件夹只适合内部验证。

## 原生运行的数据与备份

原生（非 Docker）运行时行情数据库默认落在用户数据目录 `~/.indicator-lab/market.duckdb`，可以用 `SELECTOR_DATA_DIR` 改到别处；数据库由这个 bundle 自己从在线数据源同步，不需要外部行情软件的任何文件或目录。

备份用户自己的研究成果用 bundle 自带的子命令：

~~~bash
indicator-lab export -o /path/to/backups     # 目标目录存在时写入 indicator-lab-backup-<时间戳>.zip
indicator-lab manifest <zip>                 # 只查看归档内容，不写入
indicator-lab import <zip>                   # 合并导入，已存在的记录跳过
~~~

归档只包含回测、选股、策略发现和复核记录这四类用户资产，行情和指标缓存不在其中——它们可以重新同步或重算。原生部署同样可以跑独立的 strategy-worker：backend 用 `SELECTOR_STRATEGY_WORKER_TOKEN_FILE` 指定 token 文件并用 `SELECTOR_STRATEGY_WORKER_TOKEN_AUTOGENERATE=1` 允许首次启动自动生成，worker 端设置同一个 `SELECTOR_STRATEGY_WORKER_TOKEN_FILE`，并用 `SELECTOR_STRATEGY_WORKER_API_URL` 指向 backend（默认值 `http://backend:8123` 只适用于 Compose 网络）。

## 运行前端

Nuitka bundle 只包含 Python backend 和 strategy-worker。原生分发如果不使用 Docker，需要另外构建并提供前端：

~~~bash
cd web-ui
npm ci
npm run build
~~~

生成的 web-ui/dist 需要由 Nginx 或其他 HTTP server 提供，并把 /api 请求代理到 backend 的 8123 端口。保护版 Docker 已经通过 Dockerfile.frontend 完成这一步。

## 发布检查清单

- [ ] 使用目标平台和目标架构构建；
- [ ] requirements.lock 和 requirements-build.txt 版本已确认；
- [ ] 运行 sh scripts/verify_ci.sh python；
- [ ] 运行 sh scripts/verify_ci.sh protected 或对应平台的 bundle 检查；
- [ ] 产物内没有 .py、.pyc、.pyo 或项目源码目录；
- [ ] backend /healthz 正常；
- [ ] strategy-worker 可以领取并完成一个受控测试任务；
- [ ] 使用一次受控的在线同步验证行情写入，再跑通回测和策略实验；
- [ ] 用 export/import 往返验证用户资产备份（行情和指标缓存不应出现在归档里）；
- [ ] 确认首次启动自动生成了 worker token，且容器重建后数据卷内容仍在；
- [ ] Windows 版本完成 VC++ 运行库、签名和安装器测试；
- [ ] macOS 版本完成 libomp、代码签名和 notarization 测试；
- [ ] 发布包只包含编译产物、前端静态文件、配置示例、许可证和第三方声明；
- [ ] 不发布源码、Docker build context、builder stage 或 Nuitka 缓存。

## 相关文件

- [Dockerfile.protected](../Dockerfile.protected)
- [docker-compose.yml](../docker-compose.yml)
- [docker-compose.bind.yml](../docker-compose.bind.yml)
- [docker-compose.protected.yml](../docker-compose.protected.yml)
- [build_nuitka.py](../scripts/build_nuitka.py)
- [nuitka_entrypoint.py](../scripts/nuitka_entrypoint.py)
- [verify_nuitka_bundle.py](../scripts/verify_nuitka_bundle.py)
- [独立分发部署](standalone-deployment.md)
- [Nuitka standalone 文档](https://nuitka.net/user-documentation/tutorial-setup-and-build.html)
