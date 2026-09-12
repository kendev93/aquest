# TDX 上游协议审计与解耦记录

审计日期：2026-08-30

本项目使用 Docker 中的 AKShare 作为默认股票在线行情源，TDX 仅作为显式遗留兼容源保留。本文件主要记录 TDX 协议核对结果，以及两个 provider 的边界，便于后续维护者在上游变化时重新验证。

AKShare 股票同步使用 `stock_info_a_code_name` 获取代码/简称，使用 `stock_zh_a_hist(period="daily", adjust="")` 获取未复权日线。业务层只接收项目自己的规范化 DataFrame，不直接暴露 AKShare 对象；provider、版本、列表指纹和同步错误会写入 DuckDB 元数据。

## 1. 参考版本和变更范围

此前对成熟实现的稳定版本和近期变更做过协议审计。近期变化主要集中在服务器健康评分、空 K 线响应处理、MAC/扩展行情和资金流等能力；本项目只需要 SH/SZ 日线同步，因此没有把上游 Web、CLI、MAC、资金流、扩展市场、筛选或回测模块带入运行时。

协议和离线文件行为的参考入口：

- `Market`：深市为 `0`，沪市为 `1`；
- `KlineCategory.DAY`：值为 `4`；
- `get_security_list_all()`：按市场、每页最多 1000 条取得证券列表；
- `get_security_bars(market, code, DAY, start, count)`：单次最多请求 800 根日线，`start=0` 为最新窗口；
- `.day`：小端序 32 字节记录，字段依次为 `YYYYMMDD`、开高低收整数、成交额 float32、成交量整数和保留字段。

## 2. 项目内置的最小实现

所有实现位于 `selector_app/tdx_protocol/` 和 `selector_app/market_data/`：

- `tdx_protocol.transport.TdxConnection`：TCP 建连、初始化帧、长度读取、压缩帧解包、超时和连接错误；
- `tdx_protocol.commands`：证券数量、证券列表和日线请求构造；
- `tdx_protocol.codec`：日期、价格变长整数、成交量自定义浮点和证券列表字段解码；
- `tdx_protocol.client.TdxClient`：有限重试、已配置服务器故障转移、列表分页和日线 DataFrame 转换；
- `market_data.day_format`：32 字节 `.day` 只读解析、证券类型判断、价格/成交量系数和临时 K 线状态；
- `market_data.day_importer.LocalDayImporter`：文件指纹、全量/增量导入、错误保留旧数据和缺失标记；
- `market_data.store.DuckDbMarketDataStore`：本地/在线来源优先级、批量查询、去重和 schema 状态。

响应处理有以下安全边界：

1. 空 K 线 body 或只有数量头时返回空列表，让客户端继续尝试其它服务器；
2. 响应声称的条数超过实际 body 时只返回已完整解码的前缀，不使用残缺记录；
3. 帧长度、压缩长度或初始化帧损坏时转为连接错误，进入有限重试/故障转移；
4. 指数日线每条记录的涨跌家数附加字段会被跳过，避免下一条记录错位。

## 3. `.day` 系数和证券类型

项目不把未知代码段按股票兜底。当前识别范围与已审计实现一致：

| 市场 | 类型 | 代码段 | 价格系数 | 成交量系数 |
| --- | --- | --- | ---: | ---: |
| SH | A 股 | 60、68 | 0.01 | 0.01 |
| SH | B 股 | 90 | 0.001 | 0.01 |
| SH | 指数 | 00、88、99 | 0.01 | 1.0 |
| SH | 基金/ETF | 50、51、52、53、55、56、58 | 0.001 | 1.0 |
| SH | 债券/回购 | 01、10、11、12、13、14、20 | 0.001 | 1.0 |
| SZ | 股票/B 股 | 00、20、30 | 0.01 | 0.01 |
| SZ | 指数 | 39 | 0.01 | 1.0 |
| SZ | 基金/ETF | 15、16、17、18 | 0.001 | 0.01 |
| SZ | 债券 | 10、11、12、13、14 | 0.001 | 1.0 |

金额字段保持 `.day` 中的 float32 元值；价格不做复权。业务层使用统一的 `stock/fund/index/bond` 类型，并为非股票类型标记虚拟研究回测。

## 4. 明确不移植的能力

项目没有移植或调用以下上游模块：

- Web 路由、CLI、MAC、分钟线、海外/扩展市场、资金流和财务数据；
- 上游的 `SignalScanner`、策略对象、回测引擎、组合引擎和绩效分析器；
- 上游离线写回 `.day` 的 append/sync API。

业务代码只接收项目自己的 `InstrumentRef`/`StockRef`、规范化 DataFrame、`MarketDataRepository` 和回测结果。在线同步直接写 DuckDB，本地导入才读取用户的 `.day`；任何运行时路径都不会修改源文件。

## 5. AKShare 变更检查清单

1. 验证代码名称列表仍包含代码和简称字段，并检查沪深股票数量没有异常骤降；
2. 用固定响应 fixture 验证中文日期、开高低收、成交量和成交额字段转换；
3. 验证 `adjust=""` 的原始价格口径不会与现有本地 `.day` 数据混用；
4. 验证限流、超时、空响应、重复日期和单标的失败隔离；
5. 在 Docker 中通过 `pip check`、导入检查和 provider 能力接口确认依赖与版本。

## 6. TDX 升级/协议变更检查清单

若未来需要重新参考上游版本或 TDX 服务器行为，按以下顺序验证：

1. 对照 `Market`、`KlineCategory.DAY`、请求包长度和字段偏移；
2. 用固定 fixture 验证价格差分、指数附加字段、成交量自定义浮点和空/截断响应；
3. 用 SH/SZ 股票、B 股、基金、指数和债券 `.day` fixture 验证系数；
4. 验证本地优先、在线补缺和 provisional → completed 的状态转换；
5. 运行 `pytest --cov=selector_app`、`ruff check selector_app tests`、`mypy selector_app` 和前端检查；
6. 只在协议行为确认后修改 `tdx_protocol`，不要重新引入外部行情框架作为业务依赖。
