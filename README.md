# 指标实验室（Indicator Lab）

<p align="center">
  <img src="assets/branding/indicator-lab-mark.png" alt="Indicator Lab logo" width="160">
</p>

<p align="center">
  <strong>让复杂指标可筛选、可组合、可回测。</strong><br>
  一个面向指标研究与策略验证的跨平台本地工作台
</p>

一个独立的跨平台指标筛选、组合与回测应用，使用 Vue/TypeScript + FastAPI。行情保存在本机 DuckDB，可在本项目内自行同步，在此基础上计算和验证交易指标信号。

在线股票行情默认使用项目自有客户端；项目仍内置满足 TDX 兼容同步所需的最小 TCP 客户端和 `.day` 读取器。数据接口来源及第三方许可说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

![Indicator Lab workflow](assets/branding/indicator-lab-overview.png)

## 项目起因与目标

我平时使用 macOS，但通达信的传统选股流程依赖 Windows 桌面端。为了完成每天的选股，我还需要在下班后回到 Windows 环境，这让选股和指标研究变得不方便。

另一个问题是，指标广场中的复杂指标虽然可以用于选股，但仅凭一次筛选结果，很难判断它是否真的有效；复杂指标也不容易直接进行系统回测，更难比较多个指标组合后的表现。

因此建立了指标实验室，尝试把这套工作流放到跨平台的本地应用中：

1. 输入或粘贴复杂指标，解析可用于筛选、排序和交易规则的输出；
2. 在本地行情数据上进行批量选股和多个指标组合；
3. 对单个标的、动态组合和时间切分策略进行回测与适配评估；
4. 用收益、回撤、成交样本、盈亏比和稳定性等指标判断信号，减少只凭“看起来有效”的主观判断。

项目的目标不是保证某个指标盈利，而是让指标更容易被验证、比较和复盘。项目独立实现，不隶属于通达信官方；通达信公式语法仅作为兼容的研究输入。

## 已实现能力

- 主力清洗：洗盘、主力拉高、出货、主力进场、Z；
- 庄家顶底：短期线、中期线、底部线、警戒线；
- 准备拉升：VAR1、VAR2、准备拉升、压住庄家；
- 页面支持粘贴自定义通达信公式，自动识别 `名称:=数值` 参数；命名布尔输出可作为信号，命名数值输出可用于排序和卖出规则；
- 公式选股支持“指标条件”模式：从每个指标的数值输出中选择左值，使用大于/小于/等于、上穿/下穿等关系比较固定数值或另一个指标输出；
- 指标条件可以按 AND、OR 或至少满足 N 个组合，并可命名保存到当前浏览器，之后直接加载复用；组合会同时保存预置/自定义公式、参数和筛选范围；
- AND、OR、至少满足 N 个；
- 沪深全部可识别品种、仅上海、仅深圳、自定义品种列表；
- 公式选股、组合回测、策略实验室和策略适配均可按市场、证券类型和板块配置范围；不勾选类型或板块表示不限制，两个维度同时选择时取交集；
- 扫描结果展示标的中文名；名称由在线证券列表补齐，尚未同步名称时保留代码和占位符；内置指标值只输出上述用户可选指标，自定义公式输出全部命名值；
- 支持股票、B 股、ETF/基金、指数和债券，并按品种使用对应的原始价格/成交量系数；
- 后台任务、进度、失败/跳过摘要、结果 JSON/CSV 导出；
- 动态组合回测：从公式选股池按指标排序，按固定槽位等额买入，卖出后自动用排名靠前的候选补位；品种范围可选沪深全部、仅上海、仅深圳、自定义列表文件或**指定标的**（直接填代码，只回测这几只）；建仓方式支持每槽等额或固定股数；支持每日/每周/每月刷新、百分比/ATR 止盈止损、移动止盈、独立冷却、指标阈值和指标比较卖出；
- 策略实验室：用户只设置品种范围和持仓数量，默认使用本地最新已完成日线往前 5 年寻优排序指标/方向、调仓频率、买卖条件和风险控制，也可选择全历史或自定义区间，并按验证期 Sharpe 与年化收益生成排行榜；候选可回填普通组合回测复核；
- 买卖条件研究：用户设置基础买入、卖出规则（可以只设一侧，另一侧交给研究搜索），系统对沪深全部本地标的逐只独立回测，从盈利交易与亏损交易的特征差异中挖掘阈值条件，再按 60%/20%/20% 时间切分回测验证；不构建组合、不设持仓数量上限。证券类型可在股票与 ETF 之间选择，一次只研究一类，避免跨类型样本混池导致阈值失去可比性；选择 ETF 时免印花税并使用基金佣金。结果里的候选条件可以勾选后一键追加到对应方向的基础规则并重跑一次研究；基础买入、卖出规则都可以留空：卖出留空（勾选“持有到期末”）时买入后一直持有到区间最后一天按收盘价平仓，买入留空（勾选“持续满仓”）时每次离场后下一个交易日立即再买入，研究就以这条基线为参照去搜索另一侧的时机，挖出来的候选即该侧完整规则（买入候选是 AND 上去、卖出候选是 OR 上去的，基础为空时两者都等于候选本身）。两侧不能同时留空。加载历史研究时会连同当时的标的范围、买卖规则、日期区间和交易费用一起还原，便于在旧配置上继续调整。研究还可以指定排序规则（排序指标、方向、槽位数），结果会额外给出「该按什么排序」的推荐和日均命中股票数：推荐由三条互相独立的证据支撑——平均信息系数（这个指标本身有没有预测力）、信息系数信息比率（这个准头稳不稳，界面上鼠标悬停小问号可看英文名和解释）、分段同号（是贯穿始终还是某段行情的运气），三条同向才会标为「证据充分」，否则明确降级说明原因；日均命中股票数用来判断条件在有限槽位下是否还有意义（命中 3 只时条件说了算，命中 300 只而只有 5 个槽位时真正决定买谁的是排序）。候选表格里的「前 N 名胜率」是这个判断的直接答案：它假设组合只能买前 N 名，先筛出命中该条件的股票、再按排序规则取前 N 名，只看这批真正会成交的样本的胜率；和全池胜率差得越多，说明这条结论越难直接搬到有限槽位的组合里。命中数不超过槽位数的日子排序不起作用，悬浮提示会明说这一列等于全池。点「用这套规则去跑组合回测」可把买卖规则、排序规则和槽位数一起带到组合回测页。
- 策略适配性评估：批量对股票池进行时间顺序的训练/验证/测试回测，按成交样本、期望收益、盈亏比、收益、回撤和稳定性生成透明适配分；
- 行情保存在本机 DuckDB，可在本项目发起股票、ETF、LOF 和指数的在线日线同步；
- 本地行情浏览：查看本机数据库中的品种，点击生成日线、月线、年线 K 线，支持 MA5/10/20/60、RSI14 和 MACD；
- 公式层安全除零、数据不足跳过、无未来数据；
- Python 单元/集成测试、Vue 单元测试和 Playwright E2E 流程。

应用启动时不会扫描或导入本地通达信文件。行情由本项目自己从在线数据源同步并写入本机 DuckDB，不需要任何外部行情软件。预置模式会默认选中“准备拉升”和“建仓区”；市场范围、条件组合、并发和周期收在“高级设置”中。页面会在浏览器本地记住上次配置，不会上传这些配置。

## 启动

要求 Python 3.10–3.13、Node.js 20+。推荐创建虚拟环境：

```bash
python3 -m venv .venv
source .venv/bin/activate              # Windows: .venv\\Scripts\\activate
python -m pip install --upgrade -r requirements.lock
python -m pip install -e ".[dev]"
```

启动后端：

```bash
uvicorn selector_app.web.app:app --reload --host 127.0.0.1 --port 8123
# 或：indicator-lab --reload
```

启动前端开发服务器：

```bash
cd web-ui
npm install
npm run dev
```

浏览器打开 <http://127.0.0.1:5173/indicator-lab>。动态组合回测页面是 <http://127.0.0.1:5173/portfolio-backtest>，策略实验室页面是 <http://127.0.0.1:5173/strategy-lab>，买卖条件研究页面是 <http://127.0.0.1:5173/condition-research>，策略适配性页面是 <http://127.0.0.1:5173/strategy-fitness>，本地行情页面是 <http://127.0.0.1:5173/market-data>；Vite 会把 `/api` 请求代理到 `127.0.0.1:8123`。旧的 `/formula-screen` 地址仍可访问，但不再作为主入口。

## Docker Compose 启动

如果不想在宿主机安装 Python 或 Node.js，可以直接使用 Docker：

```bash
export INDICATOR_LAB_IMAGE=<你的镜像标签>
sh scripts/start.sh
```

脚本默认带 `--build`，改完代码直接跑就会重建镜像；配置写在 `.env` 里，不用每次 export。完整命令见部署文档。

> ⚠️ 直接敲 `docker compose up -d` **不会**重建已有镜像——改了代码却忘记 `--build` 会静默跑旧镜像。

容器自带行情同步能力，不依赖任何外部行情软件。数据默认放在 Docker 命名卷 `indicator-lab_indicator-lab-data`，容器重建不会丢；想放到自己管理的宿主目录就叠加绑定挂载覆盖文件（注意该目录必须允许容器内的 `appuser`(uid 10001) 写入）：

```bash
DATA_DIR=/srv/indicator-lab docker compose -f docker-compose.yml -f docker-compose.bind.yml up -d
```

只构建镜像、不启动或重启容器：

```bash
docker compose build
```

完整的存储布局、备份恢复与升级说明见 [独立分发部署](docs/standalone-deployment.md)。

本机 DuckDB 保存 Indicator Lab 的行情缓存、回测/策略历史、策略任务状态和维护元数据。为兼容遗留文件，backend 仍会幂等导入本地数据目录中残留的旧任务 JSON 和维护记录；日常更新不需要手动执行数据迁移。DuckDB schema 升级仍会先创建备份，原有历史和行情表都会保留。

### 保护版发布镜像

开发镜像会保留 Python 源码，适合本地调试；对外分发时使用 Nuitka 保护版构建。构建阶段会编译 backend 和 `strategy-worker`，最终运行阶段只复制 standalone bundle，不复制 `selector_app` 源码：

完整的 Windows、macOS 和 Docker 打包教程见 [打包与发布指南](docs/packaging.md)。

```bash
export INDICATOR_LAB_IMAGE=<你的镜像标签>
sh scripts/start.sh --protected
```

也可以在宿主机生成 standalone bundle：

```bash
python3 -m pip install -r requirements.lock -r requirements-build.txt
python3 scripts/build_nuitka.py --jobs 4
```

保护版默认使用文件夹模式，避免大型 NumPy/Pandas 依赖每次启动都解包；`NUITKA_JOBS`、`NUITKA_LTO` 和 `NUITKA_REPRODUCIBLE` 可在构建时覆盖。发布前运行 `sh scripts/verify_ci.sh protected`，它会检查 Compose 配置、编译产物和最终镜像。Nuitka 只能提高本地逆向门槛，不能对完全控制运行机器的用户提供绝对源码保密；需要强保护时应将核心策略计算放在你的远程服务。

部署方式、数据目录、备份与恢复见 [独立分发部署](docs/standalone-deployment.md)。通达信文件导入不在本项目中执行。

数据库位于数据卷或 `DATA_DIR` 下的 `market.duckdb`，停止或删除容器不会删除它，重建镜像或容器会继续使用同一份数据。`WEB_PORT` 和 `API_PORT` 可通过 `.env` 修改。⚠️ `docker compose down -v`、`docker volume prune` 和 Docker Desktop 的 Reset 会**删除命名卷**，执行前请先导出备份（见部署文档）。

本机 DuckDB 会自动维护：同一数据库的 `.bak-*.duckdb` 备份最多保留最近一份；唯一备份超过 3 天后自动删除。应用启动时和空闲期间会检查数据库物理文件大小；达到 50% 增长阈值后，系统会用临时库和 `COPY FROM DATABASE` 重写、校验后再原子替换。维护记录保存在 DuckDB 的 `data_meta` 表中；维护失败会保留原主库不变。

## 行情配置

行情从在线数据源（East Money，免费无需密钥；另有 TDX 通路）同步股票、ETF、LOF 和指数日线并写入本机数据库。同步会先检查本地覆盖范围，自动跳过完整区间并补齐缺失区间。本项目不提供通达信文件导入。

本项目的行情页只负责按市场和代码查询 K 线，并生成日线、月线和年线图表。自定义列表仍可用于筛选和回测，但其中的行情数据来自已配置的行情数据库。

## 本地行情浏览

“本地行情”页面读取已配置行情数据库中的沪深品种，按市场和代码分页展示 K 线数量、有效区间和最新收盘价。点击品种后会生成图表；日线、月线和年线分别使用日线数据聚合，开盘取周期首日、最高取周期最高、最低取周期最低、收盘取周期末日，成交量和成交额按周期求和。

图表默认显示 MA5、MA10、MA20、MA60，也可以切换 RSI14 和 MACD（12、26、9）。服务端会先在完整本地历史上计算指标，再按所选日期范围返回，避免日期筛选导致均线预热数据丢失。为保证浏览器渲染速度，图表最多绘制最近 220 根 K 线；接口仍会返回所选区间的完整 K 线数据。当前页面提供常用技术指标，后续可以继续增加 EMA、布林带和预置/自定义通达信指标叠加。

## 公式回测

页面的“公式回测”针对一只已导入的沪深品种运行历史日线回测。预置模式可以直接选择买入和卖出输出；自定义模式先粘贴公式并解析，再分别选择两个输出。例如：

```text
N:=5;
买入:CROSS(C,REF(C,N));
卖出:CROSS(REF(C,N),C);
```

日期留空表示使用该股票全部本地历史；预置公式和已解析的自定义公式会优先读取 DuckDB 指标缓存。无论是否命中缓存，公式都基于完整历史计算，再截取所选区间，因此区间起点不会丢失 `REF`、均线等指标的预热数据。回测结果会记录本次使用的行情指纹，便于检查数据是否发生变化。默认信号在下一根 K 线开盘成交，也可以切换到下一根收盘；佣金、最低佣金、印花税和每股滑点均可调整。回测结果包含总收益、年化收益、最大回撤、夏普、胜率、成交记录、资金曲线和最近净值表。

组合回测和策略适配性两个页面的开始日期、结束日期都默认留空；两项都留空时，组合回测使用股票池的全部本地历史，策略适配性使用股票池共同可用的全部本地历史日期。策略实验室默认使用本地最新已完成日线往前 5 年，并可在页面选择全历史或自定义区间。填写后按日期区间计算，边界包含当天。回测结果与历史记录中的“数据区间”使用实际可用行情的起止日期，即使请求时没有填写日期也会保存并展示解析后的区间。

回测使用项目自有的日线交易模拟器和绩效计算器。每次成功的组合和策略适配性回测都会自动保存请求快照、完整结果、开始时间、结束时间和耗时到本地 DuckDB，分别保留最近 20 次；页面可以重新加载历史结果。交易、持仓、候选排名和适配结果会随行情档案带出标的中文名；历史名称缺失时保留市场和代码。所有品种默认使用 100 股/份、最低佣金 `0.1` 元；股票佣金 `0.0001`、印花税 `0.001`，基金/ETF、指数和债券佣金 `0.00005`、无印花税。费用按**逐项**判定：某一项仍等于股票默认值就视为未调整、回落到该品种的默认值，其余显式值照旧生效。因此 ETF 只把佣金改成万三时，印花税仍是无印花税，不会被退回股票档的千一。历史回测不代表未来收益，交易成本、涨跌停、流动性和幸存者偏差仍可能使实际结果不同，请勿将回测结果直接视为投资建议。

## 指标条件筛选

如果不想使用指标内置的固定信号，可以在公式选股页面切换到“指标条件”。页面会把每个指标的数值输出展开为可选项：主力清洗的洗盘/主力拉高/出货/主力进场/Z，庄家顶底的短期线/中期线/底部线/警戒线，准备拉升的 `VAR1`/`VAR2`/准备拉升/压住庄家，以及 K 线技术指标的均线与 MACD 三件套。每条条件支持以下形式：

```text
VAR1 上穿 8
短期线 大于 中期线
```

上穿/下穿按最新一根日线判断当前值与上一根值的关系；右侧既可以输入固定数值，也可以选择另一个指标输出。多条条件仍可用 AND、OR 或“至少满足 N 个”组合。点击“保存组合”后，配置会保存在当前浏览器中，包含条件、组合方式、公式来源和参数；它不会上传到服务器。自定义公式解析后，其命名参数和输出也会出现在同一个条件编辑器中。

其中均线（MA5/10/20/60）和 MACD 三件套是**价格量纲**：把它们当作选股条件完全没有问题（`收盘价 上穿 MA20` 是同股票内部的比较），但拿它们做跨股票排序或阈值挖掘就退化成“按股价排序”——一只 200 元的股票永远赢过 3 元的股票，池子里混入 ETF（报价集中在 0.5~5 元）时会更明显。因此这一组被排除在排序与挖掘之外，改由 K 线技术指标中的一组**无量纲输出**承担同样的信息：

| 输出 | 含义 | 覆盖的维度 |
| --- | --- | --- |
| `ATR%` | ATR(14) / 收盘价 | 波动率 |
| `量比5日` | 当日成交量 / 过去 5 日均量（均量不含当日） | 量能 |
| `乖离率20` | (收盘价 − MA20) / MA20 | 趋势偏离 |
| `20日涨幅` | 收盘价相对 20 日前的涨跌幅 | 动量 |
| `20日位置` | 收盘价在 20 日最高/最低区间中的位置，0~100 | 位置 |
| `KDJ-K` / `KDJ-D` / `KDJ-J` | 9 日随机指标，K/D 是 RSV 的 3 日平滑，J = 3K − 2D | 超买超卖 |
| `布林带%B` | (收盘价 − 下轨) / (上轨 − 下轨)，0~100；超过 100 即站上上轨 | 位置/突破 |
| `布林带宽度` | (上轨 − 下轨) / 中轨，20 日、2 倍标准差 | 波动率（挤压） |
| `WR14` | 14 日威廉指标，0 = 在区间最高、100 = 在区间最低 | 超买超卖 |
| `CCI14` | (典型价 − 14 日均值) / (0.015 × 平均绝对偏差) | 超买超卖/趋势 |
| `VR24` | (上涨量 + ½ 平盘量) / (下跌量 + ½ 平盘量) × 100 | 量能气势 |
| `20日资金流向` | 20 日带符号成交量之和 / 20 日总成交量，−100~100 | 资金流向 |
| `24日趋势R²` | log(收盘价) 对时间回归的决定系数，0~1 | 趋势线性度 |
| `年化趋势强度` | (exp(对数斜率 × 252) − 1) × R²，年化收益封顶在 −95%~+1000% | 年化趋势 × 质量 |
| `趋势质量动量` | 24 日收益 × R² / 60 日年化波动率 | 风险调整趋势 |
| `RSRS斜率` | 18 日最高价对最低价回归的 beta | 阻力/支撑强度 |
| `RSRS标准分` | beta 相对自身近一年分布的标准化得分 | 阻力/支撑（可比） |

它们都是比值或有界值，量纲不随股价变化，因此可以安全地参与跨股票排序和买卖条件研究的阈值挖掘。`年化趋势强度` 把 24 日斜率外推一年，连续涨停会外推出荒谬的量级（24 根各 +5% 得 21 万倍），因此在对数收益上做封顶，普通趋势不受影响，但最不可持续的形态不再长期霸占降序排名。KDJ、布林带、威廉和 CCI 只暴露无量纲派生量，不带价格量纲的轨线本身：判断"收盘价站上布林上轨"与判断 `布林带%B > 100` 完全等价，多留一组均线式输出只会污染排序特征。

**OBV 是唯一不能直接照搬的常见指标**：它是累积成交量的绝对值，量级随流通盘走（ETFQuest 也把它列在"不可跨标的比较"集合里）。因此这里改用它的无量纲派生量 `20日资金流向`——窗口内带符号成交量之和除以窗口总成交量，无论市值大小都落在 −100~100，同时保留"资金净流入/流出"的读法。

后面五个是趋势质量族。振荡器回答"现在处在什么位置"，趋势质量回答"这段时间走得干不干净"，后者在横截面排序上迁移得更好：`24日趋势R²` 单独看是过滤器（趋势是否像一条直线），`年化趋势强度` 是速度乘质量，`趋势质量动量` 再除以 60 日年化波动率做风险调整（波动大的股票要涨得更猛才能拿到同样的分）。注意 `RSRS标准分` 需要约一年（250 根 + 18 根回归）预热，短区间会全是空值；ETFQuest 原版用 600 根，这里下调到 250 是因为 A 股常见回测区间只有 2~3 年。

## 动态组合回测

页面的“组合回测”先对品种池计算公式，在每个刷新日筛选满足选股条件的品种，再按一个指标值排序并填充固定数量的持仓槽位。默认是每个槽位等额、按品种档案的 100 股/份整数下单；持仓不会因为排名变化被强制换仓，只有卖出规则触发后才释放槽位，下一次排名会优先补入空位。默认信号在收盘确认，并在下一根 K 线开盘执行，也可以选择下一根收盘执行。

组合配置还可以设置“适配性权重”（不使用 / 轻参考 25% / 均衡 50% / 重适配 75%）。设为非零后，系统先为每只股票生成自己的历史交易轨迹，在每个历史调仓日只使用早于当天的已平仓交易和净值计算滚动适配分，再按权重把指标排位与适配分结合成综合分参与排序。适配性差的标的只是往后排，**不会被剔除**。历史成交笔数不足时权重按 `min(1, 成交笔数 / 最少成交)` 平滑衰减，并以池内均值做中性填充，因此回测早期会自动退化为纯指标排序，不会用少量样本惩罚标的。这样既不会把完整回测结束后的成绩泄漏到过去的组合决策，也保留了“适配性不足就少买一点”的连续表达；最近候选排名中会显示综合分与适配分。

卖出规则可以组合使用：卖出信号、止损/止盈比例、某个指标达到阈值，或两个指标之间的大小关系。自定义公式解析后，命名条件可用于选股/卖出，命名数值可用于排名和指标卖出。例如：

```text
N:=20;
买入:CROSS(C,MA(C,N));
卖出:CROSS(MA(C,N),C);
强度:C/MA(C,N);
```

该版本是长仓、单一组合、固定槽位的历史模拟，不包含实盘下单、融资融券、涨跌停撮合、分红复权校准或多策略资金隔离。组合回测成功结果会按上述历史策略保存规则落入本地 DuckDB。

## 策略实验室

“策略实验室”面向日线、多标的、做多组合轮动。用户只需要设置品种范围和持仓数量 N；默认回测本地最新已完成日线往前 5 年，也可以选择全历史或自定义开始/结束日期。系统固定使用现有三个公式的用户可选数值输出，以及 MA5/10/20/60、RSI14、MACD、SIGNAL、Histogram，在有限空间内自动搜索排序指标、排序方向、刷新频率、买入/卖出条件、百分比或 ATR 风控和三类独立冷却天数。用户还可以分别设置最多 3 条必选买入/卖出指标条件；这些条件会以 AND 硬约束加入每个候选，自动搜索只填充剩余规则名额。排序只补充空槽，已有持仓不会因为排名变化被强制卖出。

实验按 60% / 20% / 20% 切分为寻优、验证和最终留出区间；验证阶段分别保留 Sharpe 目标和年化收益目标的候选池，候选哈希去重后共同完成验证。验证结果逐个进入两个独立的前十保持器：同一交易行为在夏普榜内保留夏普更高的代表，在收益榜内保留年化收益更高的代表，两个榜单互不共享去重状态，最终留出仅用于检验。真实组合回测先为本次实验准备一份只读内存映射行情/指标快照，再由自动并发策略批量评估候选：先按容器可见 CPU 形成最大并发，再根据数据量和内存预算选择实际 worker 数，避免每个进程重复加载全市场 Pandas 数据。搜索阶段只生成绩效摘要，最终应用候选时仍使用完整报告；LightGBM surrogate 默认每新增 25 个样本重训。正常模式会尽量跑满用户请求的候选预算，只有取消、超时或候选生成达到尝试上限才提前结束；内部测试和快速模式仍可显式开启提前收敛。粗筛、中筛和完整筛选之间会传递可校验的模拟检查点，状态不安全时自动从头回测。主进程负责 Optuna/LightGBM 的搜索状态和结果排序。每个候选保留完整配置、三段绩效、交易次数、胜率、换手率、复杂度、行情指纹、算法版本和随机种子，并记录唯一候选、重复提案、预检跳过数、各阶段耗时和吞吐。实验历史保存在 DuckDB，默认保留最近 20 次；“应用”会把候选配置回填到组合回测页面，重新执行并核对成交记录、绩效和数据指纹。

排行榜支持“一键回测当前榜单前十”：只复核当前正在查看的夏普榜或收益榜，使用实验原始开始日期和任务启动时本地最新已完成行情日。每个候选只执行一次全区间组合回测，再从连续资金曲线派生整体、今年以来、最近一年、最近两年和自然年度收益、回撤、Sharpe 与交易数；结果同时保存完整成交记录、资金曲线、耗时和行情指纹，行情更新会明确标记。复核结果不会反向改写验证期排行榜，同一实验和榜单默认只保留最近一次。

正式 Docker 镜像在构建时通过 `requirements.lock` 安装项目运行依赖；在线行情客户端使用 Python 标准库发起请求，不需要额外财经客户端包。`requirements.lock` 现在是最低版本依赖清单，使用 `>=` 而不是精确锁定；正式镜像缺少必需依赖时会明确提示重新构建，不会静默改变行情源或寻优算法。

可以用固定合成数据运行搜索引擎基准矩阵，不会读取或修改本地行情：

```bash
python3 scripts/benchmark_strategy_discovery.py --trials 100,500,1000 --workers 1,4,8 --output /tmp/strategy-discovery-benchmark.json
```

基准会分别覆盖冷热缓存、高低重复提案、有交易和无交易候选，并输出请求预算、唯一候选、重复提案、停止原因、阶段耗时和进程内存峰值。

## 策略适配性评估

页面的“策略适配性”用于回答“哪些标的更适合这套买卖规则”，而不是直接替代组合排序。它会批量读取股票池，每只股票使用单股票单槽位和与组合回测相同的公式、买卖、费用、滑点与下一根 K 线执行语义，然后按共同的历史日期切分为训练、验证和测试三段；三段评估彼此独立从初始资金开始，避免把前一段的持仓状态带入后一段。默认比例为 60% / 20% / 20%，可设置验证/测试期最少成交笔数和测试期最大回撤阈值。

适配分由 8 个可解释检查项组成：验证期和测试期成交数达标、两段期望收益为正、测试期盈亏比大于 1、测试期回撤在阈值内、验证期收益为正、测试期收益为正。只有验证期和测试期样本数达标且至少通过 75% 检查项时标记为“高适配”。建议先用该页面筛出适配性达标的标的，再在“组合回测”中按当前指标值排序和补位。适配性评估是研究报告；不应把使用完整历史计算出的分数直接当作过去交易时点可见的信息。

## API 快速说明

```text
GET  /api/v1/jobs/active                              # 页面刷新后恢复所有正在运行的任务
GET  /api/v1/formula-screen/metadata
POST /api/v1/formula-screen/parse
POST /api/v1/formula-screen/jobs
GET  /api/v1/formula-screen/jobs/{job_id}
GET  /api/v1/formula-screen/jobs/{job_id}/results
GET  /api/v1/formula-screen/jobs/{job_id}/export.json
GET  /api/v1/formula-screen/jobs/{job_id}/export.csv
POST /api/v1/market-data/sync-online                   # 仅在线更新
POST /api/v1/market-data/sync                          # 一键在线同步（兼容入口）
POST /api/v1/market-data/backfill                      # 在线股票历史分片补齐
GET  /api/v1/market-data/capabilities                  # 行情源能力和当前来源
GET  /api/v1/market-data/indicator-cache               # 缓存命中率、耗时和存储量
POST /api/v1/market-data/indicator-cache/precompute    # 手动预热已导入行情
GET  /api/v1/market-data/store
GET  /api/v1/market-data/jobs/active                       # 当前运行中的行情任务
GET  /api/v1/market-data/jobs/{job_id}
POST /api/v1/market-data/jobs/{job_id}/cancel             # 协作取消单个行情任务
GET  /api/v1/market-data/sync/jobs/{job_id}              # 在线/一键同步轮询
GET  /api/v1/market-data/local/instruments
GET  /api/v1/market-data/local/{market}/{code}/bars
POST /api/v1/backtests
GET  /api/v1/backtests/{job_id}
GET  /api/v1/backtests/{job_id}/results
GET  /api/v1/backtests/history
GET  /api/v1/backtests/history/{id}
DELETE /api/v1/backtests/history/{id}
POST /api/v1/portfolio-backtests
GET  /api/v1/portfolio-backtests/{job_id}
GET  /api/v1/portfolio-backtests/{job_id}/results
GET  /api/v1/portfolio-backtests/history
GET  /api/v1/portfolio-backtests/history/{id}
DELETE /api/v1/portfolio-backtests/history/{id}
GET  /api/v1/strategy-discovery/capabilities
POST /api/v1/strategy-discovery
GET  /api/v1/strategy-discovery/{job_id}
GET  /api/v1/strategy-discovery/{job_id}/results
POST /api/v1/strategy-discovery/{job_id}/cancel
GET  /api/v1/strategy-discovery/history
GET  /api/v1/strategy-discovery/history/{id}
DELETE /api/v1/strategy-discovery/history/{id}
POST /api/v1/strategy-discovery/{experiment_id}/replays
GET  /api/v1/strategy-discovery/replay-jobs/{job_id}
GET  /api/v1/strategy-discovery/replay-jobs/{job_id}/results
POST /api/v1/strategy-discovery/replay-jobs/{job_id}/cancel
GET  /api/v1/strategy-discovery/history/{experiment_id}/replays?leaderboard=sharpe|return
POST /api/v1/strategy-fitness
GET  /api/v1/strategy-fitness/{job_id}
GET  /api/v1/strategy-fitness/{job_id}/results
GET  /api/v1/strategy-fitness/history
GET  /api/v1/strategy-fitness/history/{id}
DELETE /api/v1/strategy-fitness/history/{id}
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
python -m pip install --upgrade -r requirements.lock
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

E2E 测试通过 Playwright 路由模拟 API，不需要真实行情数据库；真实页面运行需要同时启动后端和前端。

## TDX 协议和数据升级流程

1. 需要调整协议时，先阅读 [docs/upstream-api-audit.md](docs/upstream-api-audit.md) 和 `selector_app/tdx_protocol/` 的测试；
2. 修改 `.day` 系数、字段或 DuckDB schema 时，先补迁移/备份和回归测试；
3. 运行后端公式、导入、协议、回测和 API 集成测试；
4. 运行前端 typecheck/build 及 E2E；
5. 检查 `git diff`，确认没有绝对路径依赖、密钥、临时文件或提交数据库文件。

## 许可证

本项目自身源代码为陈垦版权所有的商业专有软件，不以 AGPL 或其他开源许可证发布。除非取得书面商业授权，禁止复制、修改、再分发、转售、再许可或反向工程（适用法律强制允许的范围除外）。具体权利和义务以根目录的 [LICENSE](LICENSE) 全文为准。

项目内置 TDX 协议和 `.day` 解析代码的来源、归属及第三方 MIT 许可说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
