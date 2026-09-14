# Store Repository Decomposition — TDD Evidence

## Source plan

No separate plan file was provided. The journeys below were derived from the
request to refactor the oversized DuckDB store without changing behavior.

## User journeys

- As a market-data consumer, I want the existing `DuckDbMarketDataStore` API to
  keep working after its implementation is split, so that callers do not need
  a coordinated migration.
- As a worker, I want read-only stores to keep querying and rejecting writes,
  so that parallel reads cannot accidentally mutate the database.
- As an operator, I want schema initialization, migrations, and recoverable
  backups to keep their existing behavior, so that storage upgrades remain safe.

## RED/GREEN evidence

| Stage | Command | Result |
|---|---|---|
| RED | `pytest -q tests/unit/test_store_composition.py` | FAIL during collection with the intended missing `store_bars` module |
| GREEN | `pytest -q tests/unit/test_store_composition.py tests/unit/test_store_base_backup.py tests/unit/test_duckdb_store.py` | 16 passed |
| Full verification | `pytest --cov=selector_app --cov-report=term-missing` | 275 passed; 83.66% coverage |

## Test specification

| # | Guarantee | Test or command | Type | Result |
|---|---|---|---|---|
| 1 | The facade is composed of separate bars, history, and indicator-cache repositories | `tests/unit/test_store_composition.py` | unit | PASS |
| 2 | Backup cleanup retains only the newest copy and deletes it after three days | `tests/unit/test_store_base_backup.py`, `tests/unit/test_duckdb_store.py` | unit | PASS |
| 3 | Existing market-data, history, migration, read-only, and cache behavior remains compatible | Full Python test suite | integration/unit | PASS |
| 4 | Refactored source passes formatting and lint rules | `ruff check selector_app tests` | static | PASS |
| 5 | Refactored source remains type-safe under strict mypy settings | `mypy selector_app` | static | PASS |
| 6 | Existing UI behavior remains intact | `cd web-ui && npm test -- --run` | frontend unit | PASS — 100 tests |
| 7 | Frontend types remain valid | `cd web-ui && npm run typecheck` | static | PASS |

## Implementation summary

- `store_base.py` owns DuckDB lifecycle, schema creation/migration, locking,
  frame normalization, and store status/meta operations.
- `store_bars.py`, `store_history.py`, and `store_indicator_cache.py` own their
  respective persistence domains.
- `store.py` remains the stable public facade and delegates to those domains.

## Known gaps

This change only decomposes the market-data store. The portfolio backtest and
strategy-discovery service refactors remain separate follow-up tasks. No
runtime performance claim is made; the next phase should preserve existing
backtest/search benchmark baselines.
