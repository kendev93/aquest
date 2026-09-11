# Portfolio Service Decomposition — TDD Evidence

## Source plan

No separate plan file was provided. The journeys below were derived from the
request to continue the behavior-preserving decomposition of oversized core
services.

## User journeys

- As a portfolio-backtest caller, I want the existing service entry points to
  keep working after the implementation is split, so that API and strategy
  discovery callers do not need a coordinated migration.
- As a strategy researcher, I want prepared-context, checkpoint, simulation,
  risk, and report behavior to remain consistent, so that trades and metrics do
  not change during the refactor.
- As a maintainer, I want the service responsibilities to be explicit, so that
  future changes can target one domain without editing the whole state machine.

## RED/GREEN evidence

| Stage | Command | Result |
|---|---|---|
| RED | `pytest -q tests/unit/test_portfolio_composition.py` | FAIL during collection with the intended missing `portfolio_backtest.context` module |
| GREEN | `pytest -q tests/unit/test_portfolio_composition.py tests/unit/test_portfolio_backtest.py tests/unit/test_portfolio_risk_controls.py` | 24 passed |
| Full verification | `pytest --cov=selector_app --cov-report=term-missing` | 286 passed; 83.84% coverage |

## Test specification

| # | Guarantee | Test or command | Type | Result |
|---|---|---|---|---|
| 1 | The service is composed of context, simulation, and report mixins | `tests/unit/test_portfolio_composition.py` | unit | PASS |
| 2 | Existing portfolio trades, ranking, checkpoint continuation, and reports remain compatible | `tests/unit/test_portfolio_backtest.py` | unit | PASS |
| 3 | Stop-loss, take-profit, trailing-stop, ATR, and cooldown semantics remain compatible | `tests/unit/test_portfolio_risk_controls.py` | unit | PASS |
| 4 | Strategy discovery and fitness callers keep using the same service and checkpoint exports | Full Python test suite | integration/unit | PASS |
| 5 | Refactored Python source passes lint and strict type checking | `ruff check selector_app tests`; `mypy selector_app` | static | PASS |
| 6 | Existing UI behavior remains intact | `cd web-ui && npm test -- --run` | frontend unit | PASS — 102 tests |
| 7 | Frontend types remain valid | `cd web-ui && npm run typecheck` | static | PASS |

## Implementation summary

- `context.py` owns adapter contracts, per-instrument series, filtering, and
  indicator/formula preparation.
- `checkpoint.py` and `simulation_models.py` own resumable and mutable trading
  state representations.
- `simulation.py` owns the portfolio state machine, order execution, ranking,
  and pending exits.
- `risk.py` owns ATR and OHLC risk triggers and serialized risk settings.
- `report.py` owns performance/report construction and portfolio metrics.
- `service.py` remains the stable orchestration facade and preserves the
  historical private method and type exports used by current callers.

## Known gaps

This change does not alter the strategy-discovery search pipeline or frontend
page composition. The simulation module is still the largest extracted file;
further splitting should wait for a concrete change in execution semantics or
additional regression fixtures.
