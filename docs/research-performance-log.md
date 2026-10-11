# 研究管线性能档案（2026-10）

全市场策略池与个股研究的性能优化记录：瓶颈定位过程、实施的改动、生产环境
实测数据与后续方向。数据均可复现，基准方法见文末；后续再做性能工作时应
先读本文，避免重复定位或推翻已验证的结论。

## 起点

全市场约 4161 只可研究标的，单股研究（3000 trials × ≤10 组规则）约 8 分钟，
串行覆盖周期约 23 天。Numba 内核已覆盖寻优的模拟热路径（指标汇总形态，
`SELECTOR_FAST_SIM` 开启），**模拟本身不是瓶颈**——时间消耗在并发默认全关、
进程内嵌线程池、逐候选全帧预处理与单股路径的进度上报上。

测量环境（单机 Docker 部署）：宿主 8 逻辑核（6 性能核 + 2 能效核）、Docker
VM 可见 8 核、内存 11.67 GiB；行情库 1.1 GB、5224 只、日线 2015-01-05 至
2026-10-09。基准股 SH601939（2857 根日线），`risk_trials=3000`、10 组规则、
收敛后实际 16519 个 coarse trial（所有组都在 min_trials=1500 提前停止，
3000 预算的一半从未用到）。

## 瓶颈定位

1. **隐形线程池**：DuckDB 连接默认按可见核数起内部线程池（`_open` 只配了
   `memory_limit`），BLAS/OpenMP 同理。实测单 worker 基线 CPU 峰值 358%
   （3.6 核）——多开股票并发时线程池相乘导致过度订阅，越开越卡，因此此前
   只敢用并发 1。
2. **单股路径的进度上报无节流**：`IndividualStockResearchService._report`
   每次调用都会位置参数式触发 `progress_callback(bounded, 100)`；全市场任务
   的 `_TickerProgress` 走池协调器有 1.5s 节流，但**单股重跑（mode=single）
   没有协调器**，每个 trial 直接一次 HTTP + 后端 DuckDB 写（10 条/秒 ≈ trial
   速率）。实测 worker CPU 只有 25-38%（在等）、backend 烧 62-90%（在写），
   coarse 阶段纯开销 1259s/股（每 trial ~77ms），把寻优整体拖慢约 4 倍。
3. **逐候选全帧预处理**：每次 `run_stock` 都重做双重深拷贝、复权、全帧
   sha256 指纹、ATR rolling、逐 bar 日期换算——同股 450 次规则对评估里这些
   只取决于 (标的, 窗口)，与规则无关。
4. **明细路径绕开内核**：`run_stock` 默认 `include_details=True`，规则对
   初筛的 450 次回测全走纯 Python 状态机（内核只接 metrics-only 形态）。
5. **spawn 开销（次要）**：每股约 11 个 spawn 进程池（规则对验证 1 + 每组
   规则寻优 1），微基准实测 ~1.1s/次，合计 ~12s/股（约 2%），全市场约 14 小时。

## 实施的改动

| 提交 | 内容 |
|---|---|
| `6ec2ad7` | P0：`SELECTOR_DUCKDB_THREADS` 线程闸（`store_base._open`，键内嵌线程整进程一致）+ worker 进程 BLAS/OMP 上限 + 逐股 `stage_durations_seconds` 落日志 |
| `ee2d1d3` | 节流修复：worker `_dispatch.progress_callback` 按 1s 时间节流，单股路径不再逐 trial HTTP；全市场路径（已有协调器节流）不受影响 |
| `7cd9fa5` | P1：`run_stock(metrics_only=True)` 走 numba 内核 + `_build_search_summary`，返回满足 `_report_metrics` 读取面的轻量报告；规则对验证、串行回退、候选指标三处调用点切换，明细消费方（benchmark/fitness）保持全明细 |
| `0206671` | P2：`_build_context` 拆为规则无关底座（帧预处理/复权/指纹/窗口/ATR/日期，按 `(标的, 窗口, ATR 周期, 公司行动 repr)` 缓存，LRU=4）+ 规则尾部（每候选）；缓存键含公司行动 repr，「每次运行看到最新除权事件」契约原样保留；批量路径显式传帧不进缓存 |

正确性保障：metrics_only 与全明细差分对拍（performance 逐键、成交序列、
指纹，并显式断言内核命中防静默回退）；底座缓存命中/双窗口/淘汰/批量绕过
四类测试；公司行动刷新契约测试原样通过；全量 CI（pytest 1701 + Ruff +
mypy strict）全绿。

## 实测数据

### 线程闸 A/B（同股同 seed，3000 trials，单进程基准）

| | 基线（全默认线程） | 上闸（threads=1 + BLAS=1） |
|---|---|---|
| SH601939 | 575s | 524s（-8.8%） |
| SZ300139 | 605s | 606s（持平） |
| worker CPU 峰值 | 358%（3.6 核） | 103%（1 核） |

结论：线程闸零代价（方差内略快），把「隐形并行」压掉后真实并发才有余量。
**先压隐形并行、再放真实并发，顺序不能反。**

### 单股生产对照（SH601939，mode=single，同 10 组规则同 16519 trials）

| 阶段 | 原始 | 节流+P1 | P2 | 累计 |
|---|---|---|---|---|
| rule_pair_validation | 134.3s | 36.6s | 23.9s | **-82%** |
| risk_search | 1864.9s | 673.1s | 621.4s | -67% |
| **total** | **2016.9s** | **733.2s** | **666.1s** | **-67%** |

逐组分解（10 组 coarse，16519 trials）定位了 risk_search 的构成：

- 修复前 coarse 阶段：墙钟 1649s，其中**真实评估仅 390s、纯开销 1259s**
  （每 trial ~77ms 的 HTTP + DuckDB 写税）；
- 修复后 coarse 阶段：墙钟 582s，真实评估 500s，开销已压到 **82s**
  （每 trial ~5ms）。

单股重跑的用户体感：约 34 分钟 → 约 11 分钟。

### spawn 开销

进程启动 + import（pandas/numba/selector_app）≈ 1.0-1.2s，DuckDB 打开可忽略。
每股 ~11 池 ≈ 12s/股（~2%），全市场 ~14 小时——**不值得单独立项**，仅在
并发放开后作为 spawn 风暴的缓解手段考虑（跨股票常驻进程池，P3）。

### 阶段占比（P2 后，单股 666s）

- risk_search **621s（93%）**：真实评估 ~500s（每 trial ~30ms 的快照评估
  路径）+ 开销 ~82s + medium/full 精筛；
- rule_pair_validation 23.9s（3.6%）；
- final_replays 15.5s、mine_conditions 4.7s、prepare 0.7s。

## 全市场推算

mode=full（全市场任务）从未受单股进度 bug 影响：用户实测均值约 8 分钟/股
（23 天覆盖口径），基准股实测 524-605s。P1+P2 都作用于 rule_pair 阶段
（-110s 上下），预计单股 **~400-420s**：

- 串行 4161 只 ≈ **18-19 天**（起点 23 天）；
- 线程闸已就位、内存峰值 ~1.2 GiB/股，`SELECTOR_STRATEGY_POOL_MAX_WORKERS`
  与 `SELECTOR_DISCOVERY_WORKERS` 开到 6-8 后 ≈ **3 天**（待实测）。

## 调参指引

| 变量 | 建议值 | 说明 |
|---|---|---|
| `SELECTOR_DUCKDB_THREADS` | 1 | 只注入策略 worker；研究查询都是单标的小查询 |
| `OPENBLAS_NUM_THREADS` 等 | 1 | worker 进程内已默认 |
| `SELECTOR_STRATEGY_POOL_MAX_WORKERS` | 核数 × 0.7~1 | 回测是数组密集型，内存带宽先于核数成为天花板 |
| `SELECTOR_DISCOVERY_WORKERS` | 与上同步放大 | 评估进程受共享预算约束（`EvaluationWorkerBudget`） |

## 决策记录

- **P5（risk_trials 降回 1000）不采纳**（2026-10-11 用户决策）：提前退出
  机制保证多数组在 min_trials=1500 停止，3000 预算虽然一半闲置，但保持
  现状给收敛留了空间；数据已归档，未来若重新评估可直接引用。
- **P3（spawn 池复用）后置**：实测 12s/股（~2%）低于立项门槛（<10s/股则
  后置，实测处于边缘）；真正价值在并发放开后的 spawn 风暴缓解。

## 后续方向（未立项）

1. **并发放开**：零开发，改环境变量即可，是当前投入产出比最高的一步；
   开完跑一轮全市场实测，再决定是否继续深挖。
2. **快照评估路径解剖**（~30ms/trial）：`contexts_for_config` 切片、规则
   掩码（当前对全历史求值再切窗口）、内核模拟的占比未拆分；若并发后仍不
   满意再立项。
3. 规则掩码只评窗口：`evaluate_rule_group` 目前对全历史求值后切窗，理论
   上可只评窗口（需确认指标预热语义）。

## 基准方法

- 单股全流程：`scripts/benchmark_research_search.py`（只读库，evaluation_workers=1）；
  A/B 时缩减符号数，种子固定保证同 trial 数。
- 生产对照：`POST /api/v1/strategy-pool/jobs`（mode=single）+ worker 日志的
  `个股研究阶段耗时` 行 + 任务结果 JSON 里的 `risk_search_runs` 分组元数据。
- 资源采样：`docker stats --no-stream` 每 10-15s（worker/backend 分开）；
  后端请求量用 `docker compose logs backend` 按 URL 聚合计数。
- 注意：risk_search 阶段有 ±10% 的运行间波动，跨轮对比必须同股同 seed 同
  trial 数；`nproc` 会读 `OMP_NUM_THREADS`，在已上闸的容器里显示 1，不代表
  真实核数。
