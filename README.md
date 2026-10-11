# 指标实验室（Indicator Lab）

<p align="center">
  <img src="assets/branding/indicator-lab-mark.png" alt="Indicator Lab logo" width="160">
</p>

<p align="center">
  <strong>让复杂指标可筛选、可组合、可回测。</strong><br>
  一个面向指标研究与策略验证的跨平台本地工作台
</p>

一个独立的跨平台指标筛选、组合与回测应用，使用 Vue/TypeScript + FastAPI。行情保存在本机 DuckDB，可在本项目内自行同步，在此基础上计算和验证交易指标信号。

在线股票行情默认使用项目自有客户端；项目内置 `.day` 读取器用于本地文件导入。数据接口来源及第三方许可说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

![Indicator Lab workflow](assets/branding/indicator-lab-overview.png)

## 解决什么问题

想回答「某个指标到底有没有用」，通常卡在三道坎上：

- **流程绑在特定平台上**：通达信的传统选股依赖 Windows 桌面端，换台机器或换个系统就走不下去；
- **结论停在一次筛选上**：一天跑出几十只票看着像那么回事，但没有任何东西能说明这套条件长期是否真的有效；
- **缺少统一的比较口径**：指标与指标之间、组合与组合之间没法在同一套成本和规则下横向比较，最后往往退化成「看起来有效」。

本项目把这套流程搬到一个跨平台的本地工作台里：行情自己同步进本机 DuckDB，粘贴或自定义的通达信公式会被解析成可用于筛选、排序和交易规则的输出，由此在全市场 A 股上批量选股、组合多个指标，并对单个标的、动态组合和时间切分策略做回测。

判断因此不必再凭感觉——收益、回撤、成交样本、盈亏比和稳定性都由系统算出来，你看到的是一份可以被质疑和复算的结果，而不是一张孤立的选股名单。

**项目不试图保证某个指标盈利，它的目标是让指标更容易被验证、比较和复盘。** 它不隶属于通达信官方，通达信公式语法仅作为兼容的研究输入。

## 能做什么

应用共 16 个页面，大致按「产生想法 → 验证 → 落地观察」串起来。启动后路径前缀为 `http://127.0.0.1:5173`：

| 路径 | 页面 | 它解决什么 |
|---|---|---|
| `/indicator-lab` | 公式选股 | 把交易想法拆成可勾选的条件，在全部 A 股最新日线上扫出当天同时满足的票 |
| `/condition-research` | 买卖条件研究 | 给一条基础买卖规则，系统逐只独立回测，从盈亏交易的特征差异中挖掘候选阈值条件 |
| `/portfolio-backtest` | 动态组合回测 | 按信号产生候选、按指标排名，用固定槽位等额买入，卖出后由排名靠前的候选补位 |
| `/strategy-lab` | 策略实验室 | 只说「在什么范围买几只票」，排序指标、买卖条件、风控参数由系统自动寻优并排出榜单 |
| `/strategy-fitness` | 策略适配性 | 同一套规则在每只票上按训练/验证/测试三段回测，给出可拆解的适配分 |
| `/individual-research` | 个股研究 | 用一只 A 股的全部本地历史，挖掘买卖条件、寻优退出风控并回测最终候选 |
| `/swing-research` | 波段研究 | 把行情切成涨跌波段，看波段起点附近哪些指标出现过明显不同的取值 |
| `/strategy-pool` | 全市场策略池 | 为每只本地 A 股分别研究年化与夏普最佳策略，再用共同留出段检验等权组合 |
| `/factor-test` | 因子检验 | 把每天每只票当成样本，看指标排序能否把未来若干天的收益排出先后 |
| `/factor-attribution` | 因子归因 | 拿一次跑完的回测，比较赚钱与亏钱的单子在买入信号日的指标取值差异 |
| `/instrument-lab` | 标的检验台 | 把注意力收到一只票：指标历史位置、规则回测结果、已通过检验的条件是否成立 |
| `/candidate-library` | 候选库 | 持久化保存研究推荐出来的候选条件，带完整证据快照，可采纳、淘汰、发布成策略 |
| `/history-compare` | 回测历史对比 | 把做过的实验摆在一起：归一化曲线叠绘加关键指标并排 |
| `/strategy-tracking` | 策略追踪 | 把已保存策略作为参考，从追踪日启动观察账户，每交易日收盘后重算 |
| `/market-data` | 本地行情 | 行情仓库入口：查看覆盖到哪天、同步日线、导入通达信文件、健康检查与 K 线查看 |
| `/user-manual` | 使用手册 | 汇总各页能回答什么、不能回答什么，以及每个数字的口径；网页版支持搜索 |

每个页面「能回答什么、不能回答什么、数字是怎么算的」都写在 [docs/user-manual.md](docs/user-manual.md)；应用内每个页面顶部的「这个功能怎么用」是同一份内容，三处由 `web-ui/src/data/pageGuides.json` 统一生成。

**研究范围只有 A 股**（主板、科创板、创业板），并按板块套用对应的原始价格/成交量系数。ETF/基金、指数、债券和 B 股都不进候选池：大量 ETF 跟踪同一指数，指标排序下会选出高度雷同的标的；指数本身不可交易；B 股流动性枯竭，回测里「次日开盘成交」的假设在它身上最不成立。

## 启动

要求 Python 3.10–3.13、Node.js 22.22+。推荐创建虚拟环境：

```bash
python3 -m venv .venv
source .venv/bin/activate              # Windows: .venv\Scripts\activate
python -m pip install -e ".[dev,catalog]"
uvicorn selector_app.web.app:app --reload --host 127.0.0.1 --port 8123   # 或 indicator-lab --reload
```

前端另开一个终端：

```bash
cd web-ui
npm install
npm run dev
```

浏览器打开 <http://127.0.0.1:5173/indicator-lab>。Vite 会把 `/api` 请求代理到 `127.0.0.1:8123`。

### Docker Compose

不想在宿主机装 Python 和 Node.js 就用 Docker。两种方式二选一。

**方式一：用已发布的镜像（推荐）**。镜像由 CI 构建并发布到 GitHub 容器仓库，同时提供 amd64 与 arm64，拉取即可运行，跳过本地构建：

```bash
export INDICATOR_LAB_IMAGE=ghcr.io/kendev93/indicator-lab:latest
export INDICATOR_LAB_FRONTEND_IMAGE=ghcr.io/kendev93/indicator-lab-frontend:latest
docker compose pull
docker compose up -d --no-build
```

`--no-build` 表示直接用刚拉下来的镜像，不再从源码构建。把两个变量写进 `.env` 后就不用每次 export，升级时同样执行 `pull` + `up -d` 即可。

**方式二：从源码本地构建**：

```bash
export INDICATOR_LAB_IMAGE=indicator-lab:local
sh scripts/start.sh
```

`INDICATOR_LAB_IMAGE` 是本地构建的镜像名，起个自己认得的名字即可，不是去拉取别人的镜像；脚本默认带 `--build`，改完代码直接跑就会重建镜像。

启动后在浏览器打开 <http://127.0.0.1:5173/indicator-lab>。

配置项见 [`.env.example`](.env.example)；部署细节、数据目录、备份恢复、行情数据源与通达信导入见 [docs/standalone-deployment.md](docs/standalone-deployment.md)。

## 文档

| 文档 | 内容 |
|---|---|
| [docs/user-manual.md](docs/user-manual.md) | 16 个页面的用法、口径与边界（自动生成） |
| [docs/standalone-deployment.md](docs/standalone-deployment.md) | 部署、数据目录、备份恢复、行情源、通达信导入、数据清理 |
| [docs/architecture.md](docs/architecture.md) | 系统架构、API 清单，以及跨股票可比的指标输出表 |
| [docs/research-performance-log.md](docs/research-performance-log.md) | 研究管线性能档案：瓶颈定位、实测数据、调参指引与后续方向 |
| [docs/packaging.md](docs/packaging.md) | 依赖锁定与打包 |
| [docs/upstream-api-audit.md](docs/upstream-api-audit.md) | 在线行情接口审计 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 开发环境、验证流程、提交规范与贡献授权 |

## 许可证

本项目源代码以 [GNU Affero General Public License v3（AGPL-3.0）](LICENSE) 发布：你可以自由地使用、学习、修改和再分发；但若将修改后的版本作为网络服务对外提供，必须向服务使用者开放该版本完整的源代码。具体权利和义务以根目录的 [LICENSE](LICENSE) 全文为准。

项目内置 `.day` 解析代码的来源、归属及第三方 MIT 许可说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

欢迎提交 Issue 与 Pull Request。**向本项目提交 PR 即视为你同意：该贡献以 AGPL-3.0 授权项目使用，同时授权项目维护者将该贡献随项目以其他许可证（包括未来的商业许可）发布。** 你保留对自身贡献的完整版权，本条款不改变你对自己原始作品的权利。
