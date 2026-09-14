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
python3 -m pip install -r requirements.lock -r requirements-build.txt
~~~

requirements-build.txt 固定 Nuitka、ordered-set 和 zstandard 的构建版本。保护版 Docker 会把这些依赖留在 builder 阶段，不复制到最终运行镜像。

通用构建脚本为：

~~~text
scripts/build_nuitka.py
scripts/nuitka_entrypoint.py
scripts/verify_nuitka_bundle.py
~~~

统一入口支持两个模式：

~~~text
indicator-lab server   启动 FastAPI backend
indicator-lab worker   启动 strategy-worker
~~~

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

### 构建

~~~bash
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

~~~bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.protected.yml \
  up -d
~~~

跨平台运行时推荐使用共享启动脚本，它会自动匹配 ETFQuest 数据库目录并注入挂载路径：

~~~bash
./scripts/start_shared.sh --protected
~~~

Windows PowerShell 使用 `.\scripts\start_shared.ps1 -Protected`。

这套 Compose 使用业务 `market_data` 数据卷；发布模式另外挂载共享行情数据库，backend 需要可写以支持显式全市场同步，strategy-worker 仍只读。不要在发布验证中使用用户的生产数据卷；应使用受控副本或临时 volume。

### 发布前检查

~~~bash
sh scripts/verify_ci.sh protected
docker compose -f docker-compose.yml -f docker-compose.protected.yml config --quiet
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
python -m pip install -r requirements.lock -r requirements-build.txt
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
python -m pip install -r requirements.lock -r requirements-build.txt
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

arch -x86_64 python -m pip install -r requirements.lock -r requirements-build.txt
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
- [ ] 使用受控行情副本验证导入、回测和策略实验；
- [ ] Windows 版本完成 VC++ 运行库、签名和安装器测试；
- [ ] macOS 版本完成 libomp、代码签名和 notarization 测试；
- [ ] 发布包只包含编译产物、前端静态文件、配置示例、许可证和第三方声明；
- [ ] 不发布源码、Docker build context、builder stage 或 Nuitka 缓存。

## 相关文件

- [Dockerfile.protected](../Dockerfile.protected)
- [docker-compose.protected.yml](../docker-compose.protected.yml)
- [build_nuitka.py](../scripts/build_nuitka.py)
- [nuitka_entrypoint.py](../scripts/nuitka_entrypoint.py)
- [verify_nuitka_bundle.py](../scripts/verify_nuitka_bundle.py)
- [Nuitka standalone 文档](https://nuitka.net/user-documentation/tutorial-setup-and-build.html)
