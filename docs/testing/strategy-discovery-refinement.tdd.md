# 策略候选二次优化 TDD 证据

## Source plan

来源：本次对话中确认的“基于父候选进行中等邻域自动搜索、允许全部参数局部变化、生成独立历史实验、同时输出夏普榜和收益榜”的开发计划。

## User journeys

- 用户在策略实验榜单中选择一个候选，启动二次优化，并看到独立任务进度和新的两个排行榜。
- 二次优化从父候选配置生成局部变体，条件数量保持有效，候选不重复，并沿用父实验行情指纹。
- 用户可以在历史实验列表中识别二次优化记录及其父候选。
- 父实验或候选不存在、行情指纹变化时，系统返回可理解的失败原因。

## Evidence

| Guarantee | Test target | Result |
|---|---|---|
| 父候选可以生成中等邻域变体 | `tests/unit/test_strategy_refinement.py` | PASS |
| 父候选、两榜结果和父实验关系被保留 | `tests/unit/test_strategy_discovery_service.py` | PASS |
| 行情指纹变化会拒绝二次优化 | `tests/unit/test_strategy_discovery_service.py` | PASS |
| refinement API 创建独立历史记录并保存父 hash | `tests/integration/test_strategy_discovery_api.py` | PASS |
| 前端发送父实验 ID、候选 hash 和局部搜索预算 | `web-ui/src/api/strategyDiscoveryRefinement.spec.ts` | PASS |
| 候选榜单与详情均提供二次优化入口 | `web-ui/src/pages/StrategyDiscoveryPage.spec.ts` | PASS |

## Validation

- RED：新测试先因缺少 refinement 模块、API 函数和页面按钮失败。
- GREEN：定向 Python 16 tests passed；定向前端 12 tests passed。
- Full Python: 267 passed, 1 deprecation warning.
- Full frontend: 99 tests passed with `--maxWorkers=1`.
- Python coverage: 83.24%, minimum 80% passed.
- Frontend coverage: 80.17% function coverage, minimum 80% passed.
- `ruff check selector_app tests`: passed.
- `mypy selector_app`: passed.
- `npm run typecheck`: passed.

## Known boundary

没有自动启动浏览器或执行 Playwright；用户之前明确要求由其手动进行浏览器测试。页面组件和 API 测试已覆盖入口、提交和状态展示，部署后仍建议手动点击一次候选“二次优化”流程。
