# 指标实验室（Indicator Lab）

<p align="center">
  <img src="assets/branding/indicator-lab-mark.png" alt="Indicator Lab logo" width="160">
</p>

<p align="center">
  <strong>让复杂指标可筛选、可组合、可回测。</strong><br>
  一个面向指标研究与策略验证的跨平台本地工作台
</p>

一个独立的跨平台指标筛选、组合与回测应用，使用 Vue/TypeScript + FastAPI，将本地通达信 `vipdoc` 日线导入项目自己的 DuckDB 数据仓库，再计算和验证交易指标信号。

运行时不依赖外部行情框架。项目内置满足日线同步所需的最小 TDX TCP 客户端和 `.day` 读取器；协议适配的第三方许可说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

![Indicator Lab workflow](assets/branding/indicator-lab-overview.png)

## 项目起因与目标

我平时使用 macOS，但通达信的传统选股流程依赖 Windows 桌面端。为了完成每天的选股，我还需要在下班后回到 Windows 环境，这让选股和指标研究变得不方便。

另一个问题是，指标广场中的复杂指标虽然可以用于选股，但仅凭一次筛选结果，很难判断它是否真的有效；复杂指标也不容易直接进行系统回测，更难比较多个指标组合后的表现。

因此建立了指标实验室，尝试把这套工作流放到跨平台的本地应用中：

1. 输入或粘贴复杂指标，解析可用于筛选、排序和交易规则的输出；
2. 在本地行情数据上进行批量选股和多个指标组合；
3. 对单个标的、动态组合和时间切分策略进行回测与适配评估；
4. 用收益、回撤、成交样本、盈亏比和稳定性等指标判断信号，减少只凭“看起来有效”的主观判断。

项目的目标不是保证某个指标盈利，而是让指标更容易被验证、比较和复盘。项目独立实现，不隶属于通达信官方；通达信公式和 `vipdoc` 仅作为兼容的输入来源。

## 已实现能力

- 指标一：主力进场、洗盘、主力拉高、出货；
- 指标二：始/终、高饱和、新高突破、短中长成本关系；
- 指标三：准备拉升、压住庄家、建仓区、始/终；
- 页面支持粘贴自定义通达信公式，自动识别 `名称:=数值` 参数；命名布尔输出可作为信号，命名数值输出可用于排序和卖出规则；
- 公式选股支持“指标条件”模式：从每个指标的数值输出中选择左值，使用大于/小于/等于、上穿/下穿等关系比较固定数值或另一个指标输出；
- 指标条件可以按 AND、OR 或至少满足 N 个组合，并可命名保存到当前浏览器，之后直接加载复用；组合会同时保存预置/自定义公式、参数和筛选范围；
- AND、OR、至少满足 N 个；
- 沪深全部可识别品种、仅上海、仅深圳、自定义品种列表；
- 导入、同步、公式选股、组合回测和策略适配均可按市场、证券类型和板块配置范围；不勾选类型或板块表示不限制，两个维度同时选择时取交集；
- 扫描结果展示标的中文名；名称由在线证券列表补齐，尚未同步名称时保留代码和占位符；
- 支持股票、B 股、ETF/基金、指数和债券，并按品种使用对应的原始价格/成交量系数；
- 后台任务、进度、失败/跳过摘要、结果 JSON/CSV 导出；
- 单股票公式回测：历史区间、买卖信号、全仓/固定股数、佣金/印花税/滑点、成交记录、净值曲线和绩效指标；
- 动态组合回测：从公式选股池按指标排序，按固定槽位等额买入，卖出后自动用排名靠前的候选补位；支持每日/每周/每月刷新、止盈止损、指标阈值和指标比较卖出；
- 策略适配性评估：批量对股票池进行时间顺序的训练/验证/测试回测，按成交样本、期望收益、盈亏比、收益、回撤和稳定性生成透明适配分；
- 一键将本地 `vipdoc` 全量/增量导入项目 DuckDB，源文件始终只读；
- 在线同步最新日线到 DuckDB，只补缺和更新在线来源，不覆盖本地来源；
- 本地行情浏览：查看已导入品种，点击生成日线、月线、年线 K 线，支持 MA5/10/20/60、RSI14 和 MACD；
- 公式层安全除零、数据不足跳过、无未来数据；
- Python 单元/集成测试、Vue 单元测试和 Playwright E2E 流程。

应用启动时如果检测到 `SELECTOR_VIPDOC_PATH` 或 `/data/vipdoc` 中存在行情文件，会在后台自动执行一次增量导入；首次导入完成后，DuckDB 就可以直接用于筛选、行情浏览和回测。预置模式会默认选中“准备拉升”和“建仓区”，Docker 模式自动填入 `/data/vipdoc`；市场范围、条件组合、并发和周期收在“高级设置”中。页面会在浏览器本地记住上次配置，不会上传这些配置。

## 启动

要求 Python 3.10–3.13、Node.js 20+。推荐创建虚拟环境：

```bash
python3 -m venv .venv
source .venv/bin/activate              # Windows: .venv\\Scripts\\activate
python -m pip install -r requirements.lock
python -m pip install -e ".[dev]"
```

启动后端：

```bash
uvicorn selector_app.web.app:app --reload --host 127.0.0.1 --port 8000
# 或：indicator-lab --reload
```

启动前端开发服务器：

```bash
cd web-ui
npm install
npm run dev
```

浏览器打开 <http://127.0.0.1:5173/indicator-lab>。单股回测页面是 <http://127.0.0.1:5173/backtest>，动态组合回测页面是 <http://127.0.0.1:5173/portfolio-backtest>，策略适配性页面是 <http://127.0.0.1:5173/strategy-fitness>，本地行情页面是 <http://127.0.0.1:5173/market-data>；Vite 会把 `/api` 请求代理到 `127.0.0.1:8000`。旧的 `/formula-screen` 地址仍可访问，但不再作为主入口。

## Docker Compose 启动

如果不想在宿主机安装 Python 或 Node.js，可以直接使用 Docker：

```bash
cd indicator-lab
docker compose up --build -d
```

这样克隆项目后即可直接启动页面和 API。Docker 会自动创建 `indicator_lab_vipdoc`（原始输入）和 `indicator_lab_market`（DuckDB 主库）两个持久化卷。如果之前运行过旧版本，旧卷不会被自动删除或迁移，需要在启动前自行复制数据；如果还没有导入行情，页面仍然可以打开，但扫描和回测结果会为空。

如果本机已经安装通达信，推荐直接共享桌面端的 `vipdoc` 目录，而不是复制一份：

```bash
HOST_VIPDOC_PATH=/你的通达信目录/vipdoc \
docker compose \
  -f docker-compose.yml \
  -f docker-compose.local-vipdoc.yml \
  up --build -d
```

这种模式将桌面端目录以只读方式挂载到容器；页面点击“导入本地行情”时，应用将 `.day` 内容复制为 DuckDB 记录，不会写回这个目录。页面的 vipdoc 输入框填写：

```text
/data/vipdoc
```

无通达信模式使用持久化 named volume；普通 `docker compose down` 或删除容器不会删除数据。共享模式使用宿主机原目录作为只读输入，数据库仍保存在独立的 `market_data` 卷中。不要使用 `docker compose down -v` 或执行 `docker volume rm indicator_lab_market`，除非确认要删除本地数据库。查看卷使用 `docker volume inspect indicator_lab_market`，查看状态使用 `docker compose ps`，停止使用 `docker compose down`。`WEB_PORT` 和 `API_PORT` 仍可通过 `.env` 修改。

如果只想把已有数据复制进 Docker，而不持续共享桌面端目录，仍可使用：

```bash
sh scripts/import_vipdoc.sh /你的通达信目录/vipdoc
```

复制完成后仍需打开“本地行情”页面，点击“导入本地行情”，把 volume 中的 `.day` 原始输入导入 `market_data` 数据库卷。

## 配置 vipdoc

页面“本地行情”中的 `vipdoc` 导入目录应指向包含下列目录的通达信数据目录：

```text
vipdoc/
├── sh/lday/sh600000.day
└── sz/lday/sz000001.day
```

导入和在线同步都支持配置市场、证券类型和板块。市场可以选择沪深全部、仅上海或仅深圳；证券类型可以选择股票、基金/ETF、指数和债券；股票板块可以选择主板、科创板、创业板或 B 股。板块由 SH/SZ 代码段识别，例如 SH `60` 为主板、`68` 为科创板，SZ `00` 为主板、`30` 为创业板。类型和板块都不勾选时表示全部可识别品种，不包含任何面向单一用户的默认偏好；两者都勾选时取交集。本次范围只影响当前导入、同步或业务查询，不会自动删除数据库中已有的其它品种。

导入读取的是 `.day` 的原始价格口径；如果桌面端在收盘前已写入今日临时记录，本应用会在 15:05 前将它标记为 `provisional`，筛选和回测默认忽略，收盘后下一次导入会自动转为 `completed`。自定义列表每行支持 `SH 600000`、`SZ 000001` 或单独六位代码，空行和 `#` 注释会忽略。

页面的“一键同步行情”按钮会先增量导入本地 `vipdoc`，再通过内置的最小 TDX 客户端补充最新日线并写入 DuckDB，不会写回 `.day` 文件；已有本地来源记录优先，在线数据只补充缺失日期或更新在线来源。同步过程中页面会显示已处理数量、总标的数、进度和错误摘要。全量历史优先通过本地 `.day` 导入，在线阶段只负责最新窗口和增量更新；如果只需要在线阶段，也可以调用 `/sync-online`。

## 本地行情浏览

“本地行情”页面读取 DuckDB 中已经导入的沪深品种，按市场和代码分页展示 K 线数量、有效区间和最新收盘价。点击品种后会生成图表；日线、月线和年线分别使用日线数据聚合，开盘取周期首日、最高取周期最高、最低取周期最低、收盘取周期末日，成交量和成交额按周期求和。

图表默认显示 MA5、MA10、MA20、MA60，也可以切换 RSI14 和 MACD（12、26、9）。服务端会先在完整本地历史上计算指标，再按所选日期范围返回，避免日期筛选导致均线预热数据丢失。为保证浏览器渲染速度，图表最多绘制最近 220 根 K 线；接口仍会返回所选区间的完整 K 线数据。当前页面提供常用技术指标，后续可以继续增加 EMA、布林带和预置/自定义通达信指标叠加。

## 公式回测

页面的“公式回测”针对一只已导入的沪深品种运行历史日线回测。预置模式可以直接选择买入和卖出输出；自定义模式先粘贴公式并解析，再分别选择两个输出。例如：

```text
N:=5;
买入:CROSS(C,REF(C,N));
卖出:CROSS(REF(C,N),C);
```

日期留空表示使用该股票全部本地历史；公式会先在完整历史上计算，再截取所选区间，因此区间起点不会丢失 `REF`、均线等指标的预热数据。默认信号在下一根 K 线开盘成交，也可以切换到下一根收盘；佣金、最低佣金、印花税和每股滑点均可调整。回测结果包含总收益、年化收益、最大回撤、夏普、胜率、成交记录、资金曲线和最近净值表。

回测使用项目自有的日线交易模拟器和绩效计算器，结果只保存在当前服务进程内存，服务重启后不会保留。股票使用 100 股、佣金 0.0003、最低佣金 5 元、印花税 0.001；ETF/基金/指数/债券使用 100 份、佣金 0.00005、最低佣金 0.1 元、无印花税，并明确标记为虚拟研究回测。历史回测不代表未来收益，交易成本、涨跌停、流动性和幸存者偏差仍可能使实际结果不同，请勿将回测结果直接视为投资建议。

## 指标条件筛选

如果不想使用指标内置的固定信号，可以在公式选股页面切换到“指标条件”。页面会把三个预置指标中的命名数值输出展开为可选项，例如 `VARO7`、`VAR1`、短期成本线和中期成本线。每条条件支持以下形式：

```text
VARO7 小于 10
VAR1 上穿 8
短期成本 大于 中期成本
```

上穿/下穿按最新一根日线判断当前值与上一根值的关系；右侧既可以输入固定数值，也可以选择另一个指标输出。多条条件仍可用 AND、OR 或“至少满足 N 个”组合。点击“保存组合”后，配置会保存在当前浏览器中，包含条件、组合方式、公式来源和参数；它不会上传到服务器。自定义公式解析后，其命名参数和输出也会出现在同一个条件编辑器中。

## 动态组合回测

页面的“组合回测”先对品种池计算公式，在每个刷新日筛选满足选股条件的品种，再按一个指标值排序并填充固定数量的持仓槽位。默认是每个槽位等额、按品种档案的 100 股/份整数下单；持仓不会因为排名变化被强制换仓，只有卖出规则触发后才释放槽位，下一次排名会优先补入空位。默认信号在收盘确认，并在下一根 K 线开盘执行，也可以选择下一根收盘执行。

组合配置还可以开启“适配性过滤”。开启后，系统先为每只股票生成自己的历史交易轨迹，在每个历史调仓日只使用早于当天的已平仓交易和净值，计算滚动适配分；只有达到最低适配分、最少成交数和最大回撤要求的标的才进入当前指标排名。这样不会把完整回测结束后的成绩泄漏到过去的组合决策，最近候选排名中也会显示被过滤的原因。

卖出规则可以组合使用：卖出信号、止损/止盈比例、某个指标达到阈值，或两个指标之间的大小关系。自定义公式解析后，命名条件可用于选股/卖出，命名数值可用于排名和指标卖出。例如：

```text
N:=20;
买入:CROSS(C,MA(C,N));
卖出:CROSS(MA(C,N),C);
强度:C/MA(C,N);
```

该版本是长仓、单一组合、固定槽位的历史模拟，不包含实盘下单、融资融券、涨跌停撮合、分红复权校准或多策略资金隔离。结果同样只保存在当前服务进程内存，服务重启后不会保留。

## 策略适配性评估

页面的“策略适配性”用于回答“哪些标的更适合这套买卖规则”，而不是直接替代组合排序。它会批量读取股票池，每只股票使用单股票单槽位和与组合回测相同的公式、买卖、费用、滑点与下一根 K 线执行语义，然后按共同的历史日期切分为训练、验证和测试三段；三段评估彼此独立从初始资金开始，避免把前一段的持仓状态带入后一段。默认比例为 60% / 20% / 20%，可设置验证/测试期最少成交笔数和测试期最大回撤阈值。

适配分由 8 个可解释检查项组成：验证期和测试期成交数达标、两段期望收益为正、测试期盈亏比大于 1、测试期回撤在阈值内、验证期收益为正、测试期收益为正。只有验证期和测试期样本数达标且至少通过 75% 检查项时标记为“高适配”。建议先用该页面筛出适配性达标的标的，再在“组合回测”中按当前指标值排序和补位。适配性评估是研究报告；不应把使用完整历史计算出的分数直接当作过去交易时点可见的信息。

## API 快速说明

```text
GET  /api/v1/formula-screen/metadata
POST /api/v1/formula-screen/parse
POST /api/v1/formula-screen/jobs
GET  /api/v1/formula-screen/jobs/{job_id}
GET  /api/v1/formula-screen/jobs/{job_id}/results
GET  /api/v1/formula-screen/jobs/{job_id}/export.json
GET  /api/v1/formula-screen/jobs/{job_id}/export.csv
POST /api/v1/market-data/import-local
POST /api/v1/market-data/sync-online                   # 仅在线更新
POST /api/v1/market-data/sync                          # 一键本地导入 + 在线补缺
GET  /api/v1/market-data/store
GET  /api/v1/market-data/jobs/{job_id}
GET  /api/v1/market-data/sync/jobs/{job_id}              # 在线/一键同步轮询
GET  /api/v1/market-data/local/instruments
GET  /api/v1/market-data/local/{market}/{code}/bars
POST /api/v1/backtests
GET  /api/v1/backtests/{job_id}
GET  /api/v1/backtests/{job_id}/results
POST /api/v1/portfolio-backtests
GET  /api/v1/portfolio-backtests/{job_id}
GET  /api/v1/portfolio-backtests/{job_id}/results
POST /api/v1/strategy-fitness
GET  /api/v1/strategy-fitness/{job_id}
GET  /api/v1/strategy-fitness/{job_id}/results
```

提交任务必须至少选择一个信号或一条指标条件；`at_least` 必须给出不超过当前信号/条件数量的 `minimum_matches`。条件模式的请求示例：

```json
{
  "selected_signals": [],
  "conditions": [
    {
      "left_value": "indicator_three.varo7",
      "operator": "lt",
      "right_constant": 10
    }
  ],
  "combine_mode": "all",
  "minimum_matches": null
}
```

自定义公式先调用 `/parse`，解析成功后再提交 `/jobs`；显式常量赋值如 `N:=5` 会生成参数控件，`SIGNAL:...` 会生成输出信号。完整 API 结构见 [docs/architecture.md](docs/architecture.md)。

## 测试和质量检查

提交或推送前建议先执行与 GitHub Actions 相同的本地验证脚本。默认会运行后端、前端、E2E 和两个 Docker 镜像检查：

```bash
sh scripts/verify_ci.sh
```

如果希望 Git 自动拦截未通过的提交和推送，可以在克隆后启用项目 Hooks：

```bash
sh scripts/install_hooks.sh
```

启用后，`pre-commit` 和 `pre-push` 都会执行完整检查（包含 Docker 镜像构建）。开发过程中若只想快速检查，可以手动使用 `sh scripts/verify_ci.sh all --skip-docker`；提交前建议恢复为完整检查。

首次运行前请完成 Python 和前端依赖安装，并安装 Playwright 浏览器：

```bash
python -m pip install -r requirements.lock
python -m pip install -e ".[dev]"
cd web-ui
npm ci
npx playwright install --with-deps chromium
cd ..
```

后端：

```bash
PYTHONPATH=. pytest --cov=selector_app --cov-report=term-missing
ruff check selector_app tests
mypy selector_app
```

前端：

```bash
cd web-ui
npm test
npm run test:coverage
npm run typecheck
npm run build
npm run e2e
```

E2E 测试通过 Playwright 路由模拟 API，不需要真实 vipdoc；真实页面运行需要同时启动后端和前端。

## TDX 协议和数据升级流程

1. 需要调整协议时，先阅读 [docs/upstream-api-audit.md](docs/upstream-api-audit.md) 和 `selector_app/tdx_protocol/` 的测试；
2. 修改 `.day` 系数、字段或 DuckDB schema 时，先补迁移/备份和回归测试；
3. 运行后端公式、导入、协议、回测和 API 集成测试；
4. 运行前端 typecheck/build 及 E2E；
5. 检查 `git diff`，确认没有绝对路径依赖、密钥、临时文件或提交数据库文件。

## 许可证

本项目自己的源代码采用 [GNU Affero General Public License v3.0 or later](LICENSE)（`AGPL-3.0-or-later`）。这是一个 Copyleft 许可证：修改后的版本在分发时必须继续提供对应源代码；如果修改后的版本作为网络服务向用户提供，也需要向交互用户提供对应源代码。具体权利和义务以根目录的 [LICENSE](LICENSE) 全文为准。

本项目并不禁止商业使用；商业使用者仍需遵守 AGPL 的源代码提供和许可证保留要求。项目内置 TDX 协议和 `.day` 解析代码的来源、归属及 MIT 许可说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
