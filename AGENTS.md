# Repository Guidelines

## Project Structure

`aquest/` contains the Python backend, organized by domain; `aquest/web/` holds FastAPI setup, routers, and schemas. Backend tests live in `tests/unit/` and `tests/integration/`. The Vue 3 frontend is in `web-ui/src/`, with component and page tests beside their files and Playwright specs in `web-ui/e2e/`. Shared build, verification, packaging, and maintenance commands are under `scripts/`; product and architecture notes are under `docs/`. Local market files belong in `data/vipdoc/` and must not be committed.

## Build, Run, and Verify

Use Python 3.10–3.13 and Node 22.22+ (CI tests the Node major pinned in `Dockerfile`, currently 24; `web-ui` engines enforces the floor). For local development, install dependencies with `python -m pip install -e ".[dev,catalog]"`. The release lock targets Python 3.12; Docker and CI install it with `python -m pip install --require-hashes -r requirements.txt`. Maintain direct runtime constraints in `requirements.in` and regenerate the lock under Python 3.12 with pip-tools. Run the API with `uvicorn aquest.web.app:app --reload --host 127.0.0.1 --port 8123` (or `aquest --reload`). Start the UI with `cd web-ui && npm ci && npm run dev`.

Use `sh scripts/verify_ci.sh python` for pytest with coverage, Ruff, and mypy; use `frontend` for Vitest, type checking, build, and Playwright E2E; use `docker` for Compose and image checks. `sh scripts/verify_ci.sh` runs the full CI set. These commands require dependencies to be installed first.

Verification and committing are separate steps: a passing scope writes a stamp under `.git/aquest-verify/` keyed by the HEAD tree plus interpreter version, so `git push` re-runs the Python suite only when the content being pushed actually changed (retrying a failed push is instant). Use `AQUEST_VERIFY_FORCE=1 git push` to force a re-run; never disable the hooks with `--no-verify`.

## Coding Style and Tests

Use Python 4-space indentation, `snake_case` modules/functions, and Ruff’s 100-character line limit. Ruff checks `E`, `F`, `I`, and `UP`; mypy runs in strict mode. Keep tests named `test_*.py` under the matching unit or integration area. Python coverage must remain at or above 80%. Frontend components use PascalCase `.vue` names; Vitest specs use `.spec.ts`.

## Commits and Pull Requests

Use short typed commit prefixes, for example `feat:`, `fix:`, `refactor:`, `test:`, `docs:`, and `chore:`; concise Chinese summaries are common. PRs should explain user-visible behavior, list verification commands and outcomes, and describe schema/data migration impact. Include screenshots for UI changes when they clarify the result.

## Configuration and Data Safety

Use `.env.example` as the configuration reference. Never commit credentials, local DuckDB files, imported market data, caches, or generated build output. For schema changes, document migration and backup implications and add regression coverage.
