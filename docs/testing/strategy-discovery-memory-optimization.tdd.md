# 策略实验 OOM/502 优化 TDD 证据

**状态：** 已验证
**范围：** 资源预检、分块快照、worker 并发、共享卷任务队列、backend 重启恢复和前端网关错误处理

## User journeys

- 用户提交策略实验时，系统在超出资源预算前给出可执行的范围/并发建议，不让 backend 被 OOM kill。
- 用户提交 discovery、refinement 或 replay 后，可以通过原有 API 轮询共享卷任务状态并读取最终结果。
- worker 或 backend 重启后，已完成结果可以恢复，运行中的 stale 任务会进入明确的失败状态，不会无限轮询。
- 用户默认使用最近 5 年，也可以选择全历史或设置自定义开始/结束日期。

## RED/GREEN 记录

| 阶段 | RED 证据 | GREEN 证据 |
|---|---|---|
| 资源预算 | 缺少 `resources` 模块 | `tests/unit/test_strategy_discovery_resources.py`：4 passed |
| 分块快照 | 缺少批次常量/窗口函数 | 快照、store、adapter：24 passed |
| worker 并发 | discovery/replay 固定 8 worker | 自动 CPU 上限 + 内存预检：通过 |
| 文件队列 | 缺少 `job_store`/`worker` 模块 | job store、worker、backend：15 passed |
| API coordinator | app 没有 strategy backend state | API 集成：10 passed |
| 网关恢复/日期范围 | GET 不重试、页面无日期控件 | 前端 API/页面：19 passed |

## 关键保证

| 保证 | 验证 | 结果 |
|---|---|---|
| 超预算在创建 job 前返回结构化 422 | `tests/integration/test_strategy_discovery_api.py` | PASS |
| 快照按 warm-up/窗口读取且保留完整 fingerprint | `tests/unit/test_strategy_discovery_snapshot.py`, `tests/unit/test_duckdb_store.py` | PASS |
| 日期索引不依赖全量 Python dict | `tests/unit/test_portfolio_backtest.py` | PASS |
| discovery/refinement/replay 共用文件任务协议 | `tests/unit/test_strategy_worker.py`, `tests/unit/test_strategy_job_backend.py` | PASS |
| backend 重启后幂等保存完成结果 | `tests/unit/test_strategy_job_backend.py` | PASS |
| stale worker 任务进入 terminal failure | `tests/unit/test_strategy_job_backend.py`, `tests/unit/test_strategy_job_store.py` | PASS |
| GET 502/503/504 有限重试，POST 不重试 | `web-ui/src/api/strategyDiscovery.spec.ts` | PASS |
| 默认发送最近五年模式，且支持全历史/自定义范围 | `web-ui/src/pages/StrategyDiscoveryPage.spec.ts`, `tests/integration/test_strategy_discovery_api.py` | PASS |

## 实际验证

- Python：`sh scripts/verify_ci.sh python` → **367 passed**，覆盖率 **83.07%**，2 个既有 deprecation warnings。
- Python 静态检查：`ruff check selector_app tests` → **PASS**；`mypy selector_app` → **100 source files PASS**。
- Frontend：单元测试 **117 passed**，覆盖率 **84.71%**；`npm run typecheck` 和 `npm run build` → **PASS**；策略实验关键 E2E → **1 passed**。
- 完整 Frontend E2E：**9 passed / 1 个既有 market-data 活跃任务展示断言失败**；该失败不涉及策略实验改动，沙箱内直接运行时另受 Chromium Mach port 权限限制。
- Compose：`docker compose config --quiet`、后端/前端镜像构建 → **PASS**。
- 运行时：backend、strategy-worker、frontend 均为 **healthy**；backend/worker restart count 均为 0，`OOMKilled=false`；backend `/healthz` 返回 200。
- 当前库只读资源预检：16606845 行，约 4.64 GiB 估算峰值；自动模式会按可见 CPU 和内存预算选择 worker，5 GiB 固定预算下该规模会自动降为 1 个。

## 已知边界

- 尚未对当前全市场 1000 trials 执行完整压力实验，以避免重复消耗本地资源；运行时已完成真实数据库只读预检和 Docker 服务健康验证。
- Docker worker 的最终吞吐仍需在用户可接受的时间窗口内用受控数据副本测量；首版保持 `float64`，未启用精度压缩。
