# 指标实验室架构

## 目标

应用是单机、单用户的本地行情读取与分析工具。发布模式下 ETFQuest 拥有行情 schema；Indicator Lab 通过只读适配器消费行情，并可通过专用 writer 从本项目发起受锁保护的同步任务写入既有行情表。

```text
ETFQuest market_data.db ──只读读取──► ETFQuestMarketDataAdapter ──┐
          ▲                     ▲                                │
          └──单写合并── FullMarketSync + ETFQuestMarketDataWriter │
                                                             │
开发兼容模式的本地 DuckDB ──只读/在线同步兼容───────────────┤
                                                             ▼
                                                      行情读取层
                                                             │
              ┌──────────────────────────────┼──────────────────────────────┐
              ▼                              ▼                              ▼
       IndicatorCache                 行情页/指标                    回测与策略适配
       版本化派生指标                  DuckDb adapter                 自有交易核心
              │                              │                              │
              └─────────────── ScreenEngine / Backtest / Portfolio ─────────┘
```

## 分层规则

- `market_data/` 定义项目自己的行情模型、共享读取适配器、ETFQuest 专用 writer、同步锁、同步规划和业务适配器；共享 writer 不执行 ETFQuest schema migration。
- `indicators/` 保存可重建的版本化指标缓存；缓存不是行情事实源。
- `adapters/` 通过统一 provider 路由接入项目自有在线客户端和 TDX；在线客户端默认负责股票列表/日线，TDX 只保留为显式遗留兼容通道。
- `tdx_protocol/` 只实现在线日线同步需要的 TCP 帧、证券列表、日线请求和二进制解码；不暴露上游对象给业务层。
- `formulas/` 是无副作用的数组计算层。公式函数由 `formulas/tdx_math.py` 自己实现，输入输出均为项目内部的 NumPy/Pandas 数据。
- `screening/` 负责条件合并、批量读取、单标的容错、进度和导出，不处理网络或文件格式。
- `backtest/` 负责单标的交易模拟和绩效计算；`portfolio_backtest/` 负责动态候选、固定槽位和补位；`strategy_fitness/` 复用这些交易语义完成时间切分评估。
- `web/` 负责 Pydantic 校验、任务提交/轮询、共享锁和安全错误响应。正式 Docker 运行时，策略发现、二次优化和榜单复核由独立 `strategy-worker` 执行；worker 使用容器内认证 API 上报领取、进度、取消和结果，backend 是 DuckDB 的唯一写入者。worker 对 Indicator Lab 数据库只读，计算期指标缓存留在内存，普通任务继续使用进程内 runner。
- `web-ui/` 负责行情状态展示、同步区间表单、进度和图表；共享模式下只提供显式的全市场同步入口，不提供本地行情文件导入。

在线行情客户端是项目内置实现，不引入财经数据客户端依赖；业务层只依赖项目自己的 provider 接口和规范化 DataFrame。默认应用使用项目自有在线股票行情，TDX 作为兼容 provider。历史适配模块仍只处理项目自己的 `.day` 数据。

## DuckDB 数据层

默认数据库路径为 `${SELECTOR_DATA_DIR}/market.duckdb`；未设置环境变量时使用用户数据目录。Docker 将 `SELECTOR_DATA_DIR` 设置为 `/data/market`，并把 ETFQuest 应用数据目录下的 `indicator-lab/` bind mount 到该位置；ETFQuest 的 `market_data.db` 保持独立，Indicator Lab 不向其添加业务表。

核心表：

- `instruments`：市场、代码、中文名、品种类型、源路径、首末日期、K 线数量和源状态；
- `daily_bars`：原始价格口径的 OHLCV、成交额、来源、具体 provider、K 线状态和导入时间，`(market, code, trade_date, price_basis)` 唯一；
- `data_meta`：schema 版本、最近一次在线同步时间、provider 版本及证券列表指纹。
- `indicator_cache`：按标的、周期、价格口径、内置公式版本和行情指纹保存完整指标数组与信号数组；缓存失效后可从 `daily_bars` 重建。
- `custom_indicator_cache`：按标的、周期、价格口径、自定义公式哈希、参数哈希、公式引擎版本和行情指纹保存自定义输出。
- `strategy_discovery_history`：策略实验请求、候选榜单、算法元数据、开始/结束时间和耗时；默认保留最近 20 次实验。
- `strategy_discovery_replay_history`：当前夏普榜或收益榜前十的全行情复核结果、连续资金曲线、成交记录、分段绩效、数据指纹和复核耗时；同一实验和榜单只保留最近一次，删除实验时级联删除。
- `strategy_jobs`：策略实验、二次优化和全量复核的请求、状态、进度、结果、取消标记、worker 租约及时间戳；只由 backend 写入，旧版 `.strategy-jobs` JSON 在首次启动时幂等导入。
- `backtest_history`：单股、组合和策略适配性回测的请求快照、完整结果、摘要、开始/结束时间和耗时；按回测类型分别保留最近 20 次。

行情事实写入由 ETFQuest 或本项目的共享全市场同步任务执行，并使用同一份跨进程 lease 锁和逐标的事务；派生指标缓存可由预热任务或业务请求按需填充。读操作每次建立独立只读 DuckDB 连接。全市场筛选、组合回测和策略适配通过一次批量查询得到各标的分组，避免逐文件打开 `.day`。

## 策略实验资源与任务队列

策略实验默认使用本地最新已完成日线往前 5 个日历年；用户可以选择全历史或显式开始/结束日期。系统先统计目标日期窗口内的行情行数并估算快照和 worker 工作集。`SELECTOR_DISCOVERY_WORKERS=auto` 会先按容器可见 CPU 形成最大并发，再按 `SELECTOR_DISCOVERY_MEMORY_BUDGET_MB=auto`（容器/主机可见内存的 80%）选择实际 worker 数；也可以设置整数作为并发上限或固定内存预算。即使一个 worker 也超预算时，在创建任务前返回结构化错误，不静默截断用户选定的日期范围。

快照构建按标的批次执行：完整行情指纹单独流式计算，指标计算只读取 warm-up 加实验窗口，数值数组写入只读 memmap。worker 的标的上下文和日期索引均有界，日期查找使用排序数组而不是每个进程重复建立完整 Python 字典。

正式 Docker Compose 包含独立 `strategy-worker` 服务。backend 与 worker 共享一个宿主机本地生成的只读 bearer secret；worker 仅通过未公开到 OpenAPI 的容器内接口领取任务、上报进度/心跳/取消和完成/失败结果。所有任务状态及最终历史均由 backend 写入同一 DuckDB；过期 worker 租约会由 backend 标记为失败。容器重建不会影响宿主机上的策略任务或历史。

在线同步完成后会分批预热内置公式；已有数据库或预热未完成时，筛选和回测会按需填充缓存。缓存基于完整、有序行情的 SHA-256 指纹，公式版本变化、行情更新或价格口径变化都会自动跳过旧缓存。自定义公式按公式内容、参数和引擎版本独立缓存，不同参数不会互相复用。

 schema 版本变化或维护备份时，初始化逻辑会先生成带时间戳的 `.bak-*.duckdb` 副本，并在复制成功后只保留最近一份；该备份超过 3 天后自动清理，再执行升级。更高版本数据库会拒绝打开，避免静默破坏数据。数据库文件和备份均不提交 Git。应用空闲时会检查数据库物理文件是否比上次成功整理后的大小增长至少 50%；达到阈值后通过同目录临时库和 `COPY FROM DATABASE` 重写，校验表行数及 schema 后才原子替换，并将整理状态写入 `data_meta`。旧版 `market.maintenance.json` 会在首次维护时导入该元数据表。

## 行情来源与写入边界

发布模式只从 ETFQuest 的 `market_data`、`trade_calendar` 和 `asset_info` 读取行情，不扫描、不解析、不导入本地 `.day` 文件。全市场在线同步的网络 worker 只写宿主机本地挂载目录中的临时结果，主进程通过 `ETFQuestMarketDataWriter` 写入 ETFQuest 的既有 `market_data`、`asset_info`、`trade_calendar` 和 `system_settings` 表；不会执行共享库迁移、压缩或备份。

本地开发兼容模式仍可使用只读本地适配器或显式在线同步入口进行诊断；该模式不属于发布架构，不能把本地行情库挂载给正式共享实例。

## 在线同步

在线同步 provider 支持项目自有在线客户端和项目内置的最小 TDX 客户端。普通在线同步保留既有股票流程；全市场同步通过 East Money 目录和日线接口覆盖股票、ETF、LOF、指数，股票请求失败时切换腾讯日线接口。全市场任务先查询 ETFQuest 本地覆盖，完整区间跳过，中间缺口覆盖所选区间，首尾缺失只请求缺失范围。

- TCP 建连、初始化帧、超时、有限重试和服务器故障转移；
- `get_security_list_all`；
- `get_security_bars(..., DAY, ...)`；
- TDX 日期、价格变长整数、成交量自定义浮点和指数附加字段解码；
- 空响应和截断响应的安全处理。

在线同步不写回 `.day`。全市场 provider 使用有界 `spawn` 进程写入临时 JSON 分片，下载完成后由单一主库写者按标的事务合并；活动任务可通过 `/jobs/active` 查询并通过 `/jobs/{job_id}/cancel` 协作取消。同步同时使用 `market_data.sync.lock`，ETFQuest 和本项目任一方持锁时，另一方不会继续发起行情请求。

在线证券列表的 `name` 字段会写入 `instruments`，并在扫描结果 JSON/CSV 和页面中透传。名称刷新失败不会影响已有行情扫描，结果显示 `—` 作为占位。内置公式的扫描结果只透传注册表中的用户可选输出，公式内部中间数组仍保留在指标缓存中；自定义公式透传其命名输出。

在线同步在请求日线前应用同一套市场、证券类型和板块范围，因此未选中的品种不会产生行情请求。一键同步只拉取在线证券列表并补充在线日线，也不会被本地行情子集限制。筛选、组合回测和策略适配复用该范围模型；没有设置类型或板块过滤时使用在线证券列表中的全部可识别品种，不设置项目级默认偏好。仅供内部快速更新既有标的的 `prefer_store_targets` 模式仍保留，但不用于普通 API 或自动同步。

## 公式和回测一致性

项目自有 `REF`、`SMA`、`EMA`、`MA`、`LLV`、`HHV`、`SUM`、`COUNT`、`BARSLAST`、`CROSS` 等函数，保留现有 NaN、预热和 `SMA(..., 3.2, 1)` 语义。公式一的负分母、公式二显式 `REF(A,20)` 间隔和自定义公式的因果边界均有回归测试。

指标缓存始终基于完整历史计算，再由筛选或回测截取日期窗口；不会只对可见窗口计算，从而保留均线、`REF`、`EMA` 和穿越信号的预热语义。缓存异常只影响性能，不会替代原始行情或阻断直接计算回退。

单股交易核心使用下一根开盘/收盘成交、100 股/份整数、佣金最低收费、印花税、滑点和资金不足时的 reduce 规则。股票、基金/ETF、指数和债券默认佣金均为 `0.0001`、最低 `0.1` 元；股票印花税为 `0.001`，非股票无印花税。显式费用覆盖默认值，非股票结果标记为虚拟研究回测。

策略实验室复用组合回测的数据适配器、指标缓存和交易模拟器。它固定用户给出的 `max_positions`，只搜索排序指标/方向、刷新频率、买卖规则及风险参数；排名变化只用于补空槽。搜索候选使用 Optuna TPE，LightGBM 只作为候选提议 surrogate。真实组合回测在每次实验开始时生成一次只读内存映射行情/指标快照，自动 worker 只映射并复用这份快照，实际并发由 CPU 上限和数据/内存预检共同决定；候选阶段只重算规则掩码和组合模拟，并以摘要模式跳过持仓状态和排名事件构建；主进程在批次完成后统一更新搜索状态，surrogate 只在新增样本达到重训间隔时更新，大预算搜索在长期无改善时保守提前收敛。最终评价始终由真实组合回测完成。Docker 正式运行时两项依赖由 `requirements.lock` 安装并强制要求；确定性随机搜索仅在显式 `INDICATOR_LAB_DISCOVERY_ALLOW_FALLBACK=1` 的本地诊断模式开放。

策略实验按寻优/验证/最终留出三段运行，验证阶段分别保留 Sharpe 与年化收益目标的候选池，候选哈希去重后共同完成验证。验证结果逐个进入两个独立的前十保持器：同一交易行为在夏普榜内保留夏普更高的代表，在收益榜内保留年化收益更高的代表，两个榜单互不共享去重状态；最终留出不参与排序。用户必选买入/卖出条件会在候选生成阶段固定加入，并在快照阶段预计算条件掩码；候选只搜索剩余条件名额。完整请求、候选配置、三段指标、数据指纹、算法版本和随机种子写入 `strategy_discovery_history`，并记录自动搜索开始/结束时间和耗时，默认按最近 20 次保留。单股、组合和策略适配性任务成功后写入 `backtest_history`；列表接口只读摘要，详情接口才读取完整结果。

排行榜页面可以对当前夏普榜或收益榜前十发起独立异步全量复核。复核使用实验原始开始日期和任务启动时本地最新已完成行情日，自动共享只读行情/指标快照，并按 CPU 和资源预检选择实际并发；每个候选只模拟一次，整体、今年以来、滚动一年/两年和自然年度指标均从同一条连续资金曲线派生。复核结果不参与排行榜排序，行情指纹变化仅标记为“行情已更新”；页面可展开完整配置、资金曲线和成交原因，并把完整区间配置应用回普通组合回测。

## API

- `POST /api/v1/market-data/sync-online`（仅在线更新）
- `POST /api/v1/market-data/sync-all`（共享 ETFQuest 全市场日线同步，支持快速/自定义区间）
- `POST /api/v1/market-data/sync`（一键在线同步兼容入口）
- `POST /api/v1/market-data/backfill`（在线股票历史分片补齐）
- `GET /api/v1/market-data/capabilities`（行情源能力、版本、当前来源和全市场同步能力）
- `GET /api/v1/market-data/store`
- `GET /api/v1/market-data/jobs/active`（当前运行中的行情任务和进度）
- `GET /api/v1/jobs/active`（跨页面发现正在运行的回测、筛选和策略任务）
- `GET /api/v1/market-data/jobs/{job_id}`
- `POST /api/v1/market-data/jobs/{job_id}/cancel`（协作取消单个行情任务）
- `GET /api/v1/market-data/local/instruments`
- `GET /api/v1/market-data/local/{market}/{code}/bars`
- 既有公式选股、单股回测、组合回测和策略适配 API 保持路径不变，但只读取 DuckDB；单股、组合和策略适配分别提供 `/history`、`/history/{id}` 和删除接口，策略实验室继续提供同样的历史实验接口，并通过 `/strategy-discovery/{experiment_id}/replays`、`/replay-jobs/{job_id}`、`/replay-jobs/{job_id}/results`、`/replay-jobs/{job_id}/cancel` 和 `/history/{experiment_id}/replays` 管理前十全量复核。

行情查询接口不需要 `vipdoc_path`；发布模式下行情来自 ETFQuest 共享数据库。自定义 universe 文件仍可由筛选/回测请求指定，但其中的行情数据来自数据库。

## 测试重点

单元测试覆盖开发兼容模式下的 `.day` 32 字节读取、所有已识别品种系数、残缺尾部、临时 K 线、DuckDB 去重/批量查询、共享 writer、同步区间、跨进程锁、TDX 协议帧和响应、公式结果、逐笔交易和组合补位。集成测试覆盖在线 fake client、空库/错误路径、共享同步任务、锁竞争和全部业务读取路径。
