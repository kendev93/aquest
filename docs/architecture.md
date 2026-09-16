# 指标实验室架构

## 目标

应用是单机、单用户的本地行情分析工具。行情只保存在项目自己的 DuckDB（`market.duckdb`）里，由本项目内置的在线 provider 同步；项目既不读取也不写入任何外部行情软件的数据库，行情 schema 完全由本项目拥有。

```text
East Money / TDX 在线数据源 ──在线同步──► MarketDataSync + OnlineMarketSync
                                             │
                                             │ 有界 spawn 进程写分片
                                             ▼
                                  .sync-staging 分片 DuckDB
                                             │
                                             │ 主进程按标的事务合并
                                             ▼
                        本机 market.duckdb ──只读读取──► DuckDbMarketDataAdapter
              ┌──────────────────────────────┼──────────────────────────────┐
              ▼                              ▼                              ▼
       IndicatorCache                 行情页/指标                    回测与策略适配
       版本化派生指标                  DuckDb adapter                 自有交易核心
              │                              │                              │
              └─────────────── ScreenEngine / Backtest / Portfolio ─────────┘
```

## 分层规则

- `market_data/` 定义项目自己的行情模型、DuckDB 存储与仓库、行情适配器、在线同步的分片暂存与合并、自动同步调度、数据库维护和用户资产导入导出；它不写入任何外部数据库，也不存在跨进程同步锁。
- `indicators/` 保存可重建的版本化指标缓存；缓存不是行情事实源。
- `adapters/` 通过统一 provider 路由接入项目自有在线客户端和 TDX；East Money 是默认 provider（HTTP 接口，免费且不需要 key），TDX 作为显式兼容通道，并承担 East Money 未覆盖的品种。
- `tdx_protocol/` 只实现在线日线同步需要的 TCP 帧、证券列表、日线请求和二进制解码；不暴露上游对象给业务层。
- `formulas/` 是无副作用的数组计算层。公式函数由 `formulas/tdx_math.py` 自己实现，输入输出均为项目内部的 NumPy/Pandas 数据。
- `screening/` 负责条件合并、批量读取、单标的容错、进度和导出，不处理网络或文件格式。
- `backtest/` 负责单标的交易模拟和绩效计算；`portfolio_backtest/` 负责动态候选、固定槽位和补位；`strategy_fitness/` 复用这些交易语义完成时间切分评估。
- `web/` 负责 Pydantic 校验、任务提交/轮询、任务互斥分组和安全错误响应。正式 Docker 运行时，策略发现、二次优化和榜单复核由独立 `strategy-worker` 执行；worker 使用容器内认证 API 上报领取、进度、取消和结果，backend 是 DuckDB 的唯一写入者。worker 对 Indicator Lab 数据库只读，计算期指标缓存留在内存，普通任务继续使用进程内 runner。
- `web-ui/` 负责行情状态展示、同步范围表单、进度和图表；同步入口是一键在线同步和 East Money 全量补齐，应用不提供本地行情文件导入。

在线行情客户端是项目内置实现，不引入财经数据客户端依赖；业务层只依赖项目自己的 provider 接口和规范化 DataFrame。默认 provider 是 East Money，TDX 作为兼容 provider 覆盖其余品种。项目保留自己的 `.day` 读取器和兼容适配模块，但默认装配不使用它们，应用启动时也不会扫描或导入本地通达信文件。

## DuckDB 数据层

默认数据库路径为 `${SELECTOR_DATA_DIR}/market.duckdb`；未设置环境变量时使用用户数据目录 `~/.indicator-lab`。Docker 将 `SELECTOR_DATA_DIR` 设置为 `/data/market`，并用命名卷 `indicator-lab-data` 持久化；叠加 `docker-compose.bind.yml` 时改为 `${DATA_DIR}` 的宿主目录绑定挂载，此时宿主目录必须允许容器内 `appuser`（uid 10001）写入。这个库是行情的唯一事实源，也只由本项目写入。

核心表：

- `instruments`：市场、代码、中文名、品种类型、板块、首末日期、K 线数量、源状态和最近一次 provider；`source_path` 是兼容列，在线同步写入 NULL；
- `daily_bars`：原始价格口径的 OHLCV、成交额、来源、具体 provider、K 线状态和导入时间，`(market, code, trade_date, price_basis)` 唯一；
- `data_meta`：schema 版本、最近一次在线同步时间、provider 版本及证券列表指纹。
- `indicator_cache`：按标的、周期、价格口径、内置公式版本和行情指纹保存完整指标数组与信号数组；缓存失效后可从 `daily_bars` 重建。
- `custom_indicator_cache`：按标的、周期、价格口径、自定义公式哈希、参数哈希、公式引擎版本和行情指纹保存自定义输出。
- `strategy_discovery_history`：策略实验请求、候选榜单、算法元数据、开始/结束时间和耗时；默认保留最近 20 次实验。
- `strategy_discovery_replay_history`：当前夏普榜或收益榜前十的全行情复核结果、连续资金曲线、成交记录、分段绩效、数据指纹和复核耗时；同一实验和榜单只保留最近一次，删除实验时级联删除。
- `strategy_jobs`：策略实验、二次优化和全量复核的请求、状态、进度、结果、取消标记、worker 租约及时间戳；只由 backend 写入，旧版 `.strategy-jobs` JSON 在首次启动时幂等导入。
- `screen_history`：公式筛选的请求元数据、计数摘要和完整结果 JSON，默认保留最近 10 次。
- `backtest_history`：单股、组合和策略适配性回测的请求快照、完整结果、摘要、开始/结束时间和耗时；按回测类型分别保留最近 20 次，买卖条件研究也写入该表。

行情事实写入只由本项目的主进程执行：在线同步的 provider worker 先把规范化结果写进各自的分片数据库，再由主进程按标的事务合并；派生指标缓存可由预热任务或业务请求按需填充。库内并发写由进程内写锁和 DuckDB 连接串行化，跨进程不再有 lease 锁或锁文件，策略 worker 始终以只读方式打开同一数据库。读操作每次建立独立只读 DuckDB 连接。全市场筛选、组合回测和策略适配通过一次批量查询得到各标的分组，不逐标的往返查询。

## 策略实验资源与任务队列

策略实验默认使用本地最新已完成日线往前 5 个日历年；用户可以选择全历史或显式开始/结束日期。系统先统计目标日期窗口内的行情行数并估算快照和 worker 工作集。`SELECTOR_DISCOVERY_WORKERS=auto` 会先按容器可见 CPU 形成最大并发，再按 `SELECTOR_DISCOVERY_MEMORY_BUDGET_MB=auto`（容器/主机可见内存的 80%）选择实际 worker 数；也可以设置整数作为并发上限或固定内存预算。即使一个 worker 也超预算时，在创建任务前返回结构化错误，不静默截断用户选定的日期范围。

快照构建按标的批次执行：完整行情指纹单独流式计算，指标计算只读取 warm-up 加实验窗口，数值数组写入只读 memmap。worker 的标的上下文和日期索引均有界，日期查找使用排序数组而不是每个进程重复建立完整 Python 字典。

正式 Docker Compose 包含独立 `strategy-worker` 服务。backend 与 worker 共用数据目录里的同一个 token 文件（首次启动由 backend 自动生成），worker 只读取它作为 bearer secret；worker 仅通过未公开到 OpenAPI 的容器内接口领取任务、上报进度/心跳/取消和完成/失败结果。所有任务状态及最终历史均由 backend 写入同一 DuckDB；过期 worker 租约会由 backend 标记为失败。容器重建不会影响数据卷中的策略任务或历史。

在线同步完成后会分批预热内置公式；已有数据库或预热未完成时，筛选和回测会按需填充缓存。缓存基于完整、有序行情的 SHA-256 指纹，公式版本变化、行情更新或价格口径变化都会自动跳过旧缓存。自定义公式按公式内容、参数和引擎版本独立缓存，不同参数不会互相复用。

schema 版本变化或维护备份时，初始化逻辑会先生成带时间戳的 `.bak-*.duckdb` 副本，并在复制成功后只保留最近一份；该备份超过 `SELECTOR_DB_BACKUP_RETENTION_DAYS` 天后自动清理，再执行升级（默认 3 天；独立分发编排默认设为 0，表示压缩期间临时保留一份用于回滚、成功后立即删除）。更高版本数据库会拒绝打开，避免静默破坏数据。数据库文件和备份均不提交 Git。应用空闲时会检查数据库物理文件是否比上次成功整理后的大小增长至少 50%；达到阈值后通过同目录临时库和 `COPY FROM DATABASE` 重写，校验表行数及 schema 后才原子替换，并把上一份库以硬链接保留为备份而不是整库拷贝，因此压缩峰值是两份库大小而不是三份；整理状态写入 `data_meta`。旧版 `market.maintenance.json` 会在首次维护时导入该元数据表。

## 行情来源与写入边界

本机 `market.duckdb` 是行情的唯一事实源：只由本项目写入，也不与任何外部软件共享。应用不扫描、不解析、不导入本地通达信 `.day` 文件；项目自有的 `.day` 读取器和兼容适配模块仅为兼容保留，默认装配不启用它们。

在线同步的网络 worker 只把规范化结果写进数据目录下的 `.sync-staging/<run_id>/` 分片数据库，主进程在 worker 全部结束后按标的事务合并进主库，合并按 `(market, code, trade_date, price_basis)` 幂等去重。同一时刻只允许一个行情同步任务在跑，由任务执行器的互斥分组保证，不再需要跨进程锁文件。

## 用户资产备份与恢复

`market_data/transfer.py` 导出用户无法重建的记录：`backtest_history`（含单股、组合、策略适配和买卖条件研究）、`screen_history`、`strategy_discovery_history` 和 `strategy_discovery_replay_history` 四张表，打包成一个 zip。行情和指标缓存刻意不导出，它们总能重新同步或重算；一份完整安装里资产约 13 MB，而数据库本身在 GB 量级。

归档内容是每张表一个 JSONL 条目，外加 `manifest.json`（格式标识、格式版本、schema 版本、逐表行数与列名、导出时的行情规模）。导入是幂等合并：主键已存在的记录记为 skipped，不覆盖也不删除；备份里出现当前 schema 不认识的列、或备份来自更高的 schema 版本时，在写入前直接拒绝。上传上限为 1 GiB。

接口为 `GET /api/v1/data-transfer/manifest`、`GET /api/v1/data-transfer/export` 和 `POST /api/v1/data-transfer/import`；命令行入口为 `python -m selector_app export [-o 目标]`、`python -m selector_app import <zip>` 和 `python -m selector_app manifest <zip>`。

## 在线同步

在线同步 provider 默认是 East Money：它用内置 HTTP 客户端拉取证券列表和日线，免费且不需要密钥，目前覆盖主板、科创板和创业板股票，单只股票的日线请求失败时切换腾讯日线接口。B 股、基金/ETF/LOF、指数和债券不在 East Money 范围内，需要显式选择 TDX；范围校验不通过时在发起请求前直接报错，而不是静默跳过。同步前先查询本机库的已有覆盖，完整区间跳过，中间缺口只覆盖所选区间，首尾缺失只请求缺失范围。

内置的 TDX 兼容通道只实现日线同步需要的最小协议能力：

- TCP 建连、初始化帧、超时、有限重试和服务器故障转移；
- `get_security_list_all`；
- `get_security_bars(..., DAY, ...)`；
- TDX 日期、价格变长整数、成交量自定义浮点和指数附加字段解码；
- 空响应和截断响应的安全处理。

在线同步不写回 `.day`，也不修改任何外部文件。provider 使用有界的 `spawn` 进程池，每个 worker 把结果写进自己独立的分片 DuckDB（`market_data/staging.py`，最多 8 个 worker，分片位于数据目录的 `.sync-staging/`），全部结束后由单一主库写者按标的事务合并；活动任务可通过 `/jobs/active` 查询并通过 `/jobs/{job_id}/cancel` 协作取消。`/api/v1/market-data/capabilities` 会报告当前执行模式、worker 数和分片目录。

在线证券列表的 `name` 字段会写入 `instruments`，并在扫描结果 JSON/CSV 和页面中透传。名称刷新失败不会影响已有行情扫描，结果显示 `—` 作为占位。内置公式的扫描结果只透传注册表中的用户可选输出，公式内部中间数组仍保留在指标缓存中；自定义公式透传其命名输出。

在线同步在请求日线前应用同一套市场、证券类型和板块范围，因此未选中的品种不会产生行情请求。一键同步只拉取在线证券列表并补充在线日线，也不会被本地行情子集限制。筛选、组合回测和策略适配复用该范围模型；没有设置类型或板块过滤时使用在线证券列表中的全部可识别品种，不设置项目级默认偏好。仅供内部快速更新既有标的的 `prefer_store_targets` 模式仍保留，但不用于普通 API 或自动同步。

## 公式和回测一致性

项目自有 `REF`、`SMA`、`EMA`、`MA`、`LLV`、`HHV`、`SUM`、`COUNT`、`BARSLAST`、`CROSS` 等函数，保留现有 NaN、预热和 `SMA(..., 3.2, 1)` 语义。公式一的负分母、公式二显式 `REF(A,20)` 间隔和自定义公式的因果边界均有回归测试。

指标缓存始终基于完整历史计算，再由筛选或回测截取日期窗口；不会只对可见窗口计算，从而保留均线、`REF`、`EMA` 和穿越信号的预热语义。缓存异常只影响性能，不会替代原始行情或阻断直接计算回退。

单股交易核心使用下一根开盘/收盘成交、100 股/份整数、佣金最低收费、印花税、滑点和资金不足时的 reduce 规则。股票、基金/ETF、指数和债券默认佣金均为 `0.0001`、最低 `0.1` 元；股票印花税为 `0.001`，非股票无印花税。显式费用覆盖默认值，非股票结果标记为虚拟研究回测。

策略实验室复用组合回测的数据适配器、指标缓存和交易模拟器。它固定用户给出的 `max_positions`，只搜索排序指标/方向、刷新频率、买卖规则及风险参数；排名变化只用于补空槽。搜索候选使用 Optuna TPE，LightGBM 只作为候选提议 surrogate。真实组合回测在每次实验开始时生成一次只读内存映射行情/指标快照，自动 worker 只映射并复用这份快照，实际并发由 CPU 上限和数据/内存预检共同决定；候选阶段只重算规则掩码和组合模拟，并以摘要模式跳过持仓状态和排名事件构建；主进程在批次完成后统一更新搜索状态，surrogate 只在新增样本达到重训间隔时更新，大预算搜索在长期无改善时保守提前收敛。最终评价始终由真实组合回测完成。Docker 正式运行时两项依赖由 `requirements.lock` 安装并强制要求；确定性随机搜索仅在显式 `INDICATOR_LAB_DISCOVERY_ALLOW_FALLBACK=1` 的本地诊断模式开放。

策略实验按寻优/验证/最终留出三段运行，验证阶段分别保留 Sharpe 与年化收益目标的候选池，候选哈希去重后共同完成验证。验证结果逐个进入两个独立的前十保持器：同一交易行为在夏普榜内保留夏普更高的代表，在收益榜内保留年化收益更高的代表，两个榜单互不共享去重状态；最终留出不参与排序。用户必选买入/卖出条件会在候选生成阶段固定加入，并在快照阶段预计算条件掩码；候选只搜索剩余条件名额。完整请求、候选配置、三段指标、数据指纹、算法版本和随机种子写入 `strategy_discovery_history`，并记录自动搜索开始/结束时间和耗时，默认按最近 20 次保留。单股、组合和策略适配性任务成功后写入 `backtest_history`；列表接口只读摘要，详情接口才读取完整结果。

排行榜页面可以对当前夏普榜或收益榜前十发起独立异步全量复核。复核使用实验原始开始日期和任务启动时本地最新已完成行情日，自动共享只读行情/指标快照，并按 CPU 和资源预检选择实际并发；每个候选只模拟一次，整体、今年以来、滚动一年/两年和自然年度指标均从同一条连续资金曲线派生。复核结果不参与排行榜排序，行情指纹变化仅标记为“行情已更新”；页面可展开完整配置、资金曲线和成交原因，并把完整区间配置应用回普通组合回测。

## 买卖条件研究

买卖条件研究是独立于策略实验室的单股规则挖掘流程。用户给定的基础买入和卖出规则固定保留；买入候选作为额外 AND 过滤条件，卖出候选作为额外 OR 退出触发。所有股票分别使用同一单股交易模拟器、费用和成交语义回测，不共享资金，也不限制跨股票的持仓数。

研究任务分寻优、验证和最终留出三段。寻优段先从已完成交易抽取入场特征和持仓期退出决策样本，以净盈利交易或“提前退出优于基础退出”的样本作为目标，使用分位阈值和跨指标比较生成可读规则；卖出挖掘每笔交易最多采样 8 个均匀分布的持仓决策点，候选规则仍在完整日线上重放。候选按支持样本、覆盖股票数和相对基准胜率 lift 筛选。每只股票在每一阶段只准备一次行情和指标数据，随后在内存中复用已计算指标重跑候选规则。每只股票的行情指纹在各阶段保持一致；任务期间数据变化时终止研究，避免不同阶段混用行情。候选回测使用 summary-only 模式，只生成绩效所需的日净值和成交统计，不保存每个候选的逐日持仓明细；最终结果仍包含逐股票绩效摘要。

验证段按跨股票合并成交胜率提升、盈亏比、平均单笔净收益、成交数和覆盖股票数筛选候选；默认每侧最多挖掘 5 条规则，每侧取 1 条进入最终留出。最终留出结果不参与搜索和排名。跨股票总胜率和盈亏比按独立股票的平仓成交汇总；收益率、最大回撤和 Sharpe 只按个股计算并展示分布，不生成或暗含组合净值。

任务运行在通用后台任务执行器中，支持进度、取消和错误摘要；成功结果写入 backtest_history 的 condition_research 类型，并可从历史接口加载。逐股票的训练、验证和留出复验按股票拆分为独立任务，通过 spawn 进程池并行执行：每个 worker 自行以只读方式打开行情库、一次只处理一只股票，只回传精简报告和挖掘样本，行情聚合、条件挖掘、约束过滤和排序仍由主进程完成。worker 数由 `SELECTOR_CONDITION_RESEARCH_WORKERS`（默认 `auto`，取可见 CPU 的一半，上限 8）或显式整数决定，并按数据量做内存预算：worker 侧约 `224 MB + 2 KB × 每标的 K 线数`（实测与规模弱相关），主进程侧随股票数和候选数线性增长（实测每股票约 384 KB、每个股票候选组合约 8 KB），两者之和必须落在 `SELECTOR_CONDITION_RESEARCH_MEMORY_BUDGET_MB`（默认取容器/宿主内存的 80%）内。预算只够部分 worker 时按实际值降级；连单进程都放不下时在创建任务前返回 422 并给出估算明细，不会启动必然 OOM 的任务。无法重建的注入了自定义适配器的场景自动退回进程内执行。

## API

- `POST /api/v1/market-data/sync-online`（按所选 provider、市场和品种范围在线更新）
- `POST /api/v1/market-data/sync`（一键在线同步兼容入口，与 `sync-online` 同一实现）
- `POST /api/v1/market-data/backfill`（East Money 股票历史全量补齐，固定使用 `eastmoney` provider）
- `GET /api/v1/market-data/capabilities`（provider 能力、版本、当前来源、同步执行模式和范围）
- `GET /api/v1/market-data/store`（本机库状态、自动同步状态和最近一次同步摘要；`full_market_sync_supported` 恒为 false、`market_sync_lock` 恒为 null，仅为兼容旧客户端保留）
- `GET /api/v1/market-data/indicator-cache`（缓存命中率、耗时和存储量）
- `POST /api/v1/market-data/indicator-cache/precompute`（手动预热已同步行情的指标缓存）
- `GET /api/v1/market-data/jobs/active`（当前运行中的行情任务和进度）
- `GET /api/v1/jobs/active`（跨页面发现正在运行的回测、筛选和策略任务）
- `GET /api/v1/market-data/jobs/{job_id}`
- `GET /api/v1/market-data/sync/jobs/{job_id}`（在线/一键同步与回填的轮询入口）
- `POST /api/v1/market-data/jobs/{job_id}/cancel`（协作取消单个行情任务）
- `GET /api/v1/market-data/local/instruments`
- `GET /api/v1/market-data/local/{market}/{code}/bars`
- `GET /api/v1/data-transfer/manifest`（列出备份会包含哪些表的多少行，不生成文件）
- `GET /api/v1/data-transfer/export`（下载用户资产 zip）
- `POST /api/v1/data-transfer/import`（上传并合并备份，已存在的记录记为 skipped）
- `GET /api/v1/authorization/status`（恒定返回本机版不校验会员）
- `GET /api/v1/condition-research/capabilities`
- `POST /api/v1/condition-research`（异步启动全股票逐只规则研究）
- `GET /api/v1/condition-research/{job_id}`、`GET /api/v1/condition-research/{job_id}/results` 和 `POST /api/v1/condition-research/{job_id}/cancel`
- `GET /api/v1/condition-research/history` 与 `GET /api/v1/condition-research/history/{history_id}`
- 既有公式选股、单股回测、组合回测和策略适配 API 保持路径不变，但只读取 DuckDB；单股、组合和策略适配分别提供 `/history`、`/history/{id}` 和删除接口，策略实验室继续提供同样的历史实验接口，并通过 `/strategy-discovery/{experiment_id}/replays`、`/replay-jobs/{job_id}`、`/replay-jobs/{job_id}/results`、`/replay-jobs/{job_id}/cancel` 和 `/history/{experiment_id}/replays` 管理前十全量复核。

行情查询接口不需要 `vipdoc_path`；请求体里仍保留该字段只为兼容旧客户端，行情统一来自本机 `market.duckdb`。自定义 universe 文件仍可由筛选/回测请求指定，但其中的行情数据来自数据库。

本机版没有任何鉴权：`GET /api/v1/authorization/status` 恒定返回 `required=false`、`available=true`、`can_access_core=true`、`access_state=unrestricted`（`本机版不校验会员`）。此前跟随共享行情架构存在的机器绑定会员校验已完全移除，接口形状暂时保留，供将来接入微信小程序账号登录使用。

## 测试重点

单元测试覆盖项目自有的 `.day` 32 字节读取、所有已识别品种系数、残缺尾部、临时 K 线、DuckDB schema 与去重、批量查询和只读适配器、在线同步分片与合并幂等、同步区间收敛、自动同步调度、数据库备份与硬链接压缩、数据导入导出往返、TDX 协议帧和响应、公式结果、逐笔交易和组合补位，以及容器编排（命名卷、worker token 自动生成、不依赖外部行情软件）。集成测试覆盖在线 fake client、空库/错误路径、行情同步任务、数据备份导入导出 API 和全部业务读取路径。
