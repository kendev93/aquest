#!/bin/sh

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

usage() {
  cat <<'EOF'
用法: sh scripts/verify_ci.sh [all|python|frontend|frontend-fast|docker] [--skip-docker] [--install-playwright]

依赖需要提前安装；脚本只执行检查，不自动安装 Python/npm 依赖。
默认 all 会执行 Python、前端和 Docker 全部检查。
--skip-docker 只对 all 生效，用于本地快速检查。
--install-playwright 在前端检查前执行与 CI 相同的浏览器安装命令。
frontend-fast 只跑 Vitest 与类型检查（不含构建与 Playwright E2E），供推送前使用。
EOF
}

mode=all
skip_docker=0
install_playwright=0
compose_fixture=""
pytest_basetemp=""

for argument in "$@"; do
  case "$argument" in
    all|python|frontend|frontend-fast|docker)
      mode=$argument
      ;;
    --skip-docker)
      skip_docker=1
      ;;
    --install-playwright)
      install_playwright=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf '未知参数: %s\n\n' "$argument" >&2
      usage >&2
      exit 2
      ;;
  esac
done

cd "$REPO_ROOT"

# 验证通过后按范围盖章，pre-push 据此决定要不要重跑。
. "$SCRIPT_DIR/verify_stamp.sh"

cleanup_compose_fixture() {
  if [ -n "$compose_fixture" ] && [ -d "$compose_fixture" ]; then
    rm -r "$compose_fixture"
  fi
  compose_fixture=""
}

# pytest 会在 session 开头 rm_rf 它指定的 basetemp。整套测试共用一个固定目录时，
# 并发跑的第二个 session 会删掉第一个正在用的那个（这个仓库经常好几个会话同时改）。
# 每次运行各给一个独立目录，退出时清掉。
cleanup_pytest_basetemp() {
  if [ -n "$pytest_basetemp" ] && [ -d "$pytest_basetemp" ]; then
    rm -r "$pytest_basetemp"
  fi
  pytest_basetemp=""
}

cleanup_fixtures() {
  cleanup_compose_fixture
  cleanup_pytest_basetemp
}

prepare_compose_fixture() {
  compose_fixture=$(mktemp -d "${TMPDIR:-/tmp}/aquest-compose.XXXXXX")
}

trap cleanup_fixtures EXIT

run_python_checks() {
  printf '%s\n' '验证 Python：测试、覆盖率、格式、Ruff、mypy'
  if command -v python >/dev/null 2>&1; then
    python_bin=python
  elif command -v python3 >/dev/null 2>&1; then
    python_bin=python3
  else
    printf '%s\n' '未找到 python 或 python3' >&2
    exit 1
  fi
  pytest_basetemp=$(mktemp -d "${TMPDIR:-/tmp}/aquest-pytest.XXXXXX")
  PYTHONPATH=. "$python_bin" -m pytest --basetemp="$pytest_basetemp" --cov=aquest --cov-report=term-missing
  cleanup_pytest_basetemp
  # 工具也走同一个解释器：裸 `ruff` / `mypy` 取的是 PATH 上碰到的那一份，
  # 可能是另一个 Python 装的版本，检出来的结果和刚跑完测试的这套依赖无关。
  "$python_bin" -m ruff format --check aquest tests
  "$python_bin" -m ruff check aquest tests
  "$python_bin" -m mypy aquest
}

run_frontend_checks() {
  printf '%s\n' '验证前端：单元测试、覆盖率、类型、构建、E2E'
  (
    cd "$REPO_ROOT/web-ui"
    npm test -- --run
    npm run test:coverage
    npm run typecheck
    npm run build
    if [ "$install_playwright" -eq 1 ]; then
      npx playwright install --with-deps chromium
    fi
    npm run e2e
  )
}

run_frontend_fast_checks() {
  printf '%s\n' '验证前端（快档）：单元测试、类型检查'
  (
    cd "$REPO_ROOT/web-ui"
    npm test -- --run
    npm run typecheck
  )
}

run_docker_checks() {
  printf '%s\n' '验证 Docker：Compose 配置（默认与绑定挂载）、后端镜像、前端镜像'
  prepare_compose_fixture
  AQUEST_IMAGE=aquest-backend:ci \
    docker compose -f docker-compose.yml config --quiet
  AQUEST_IMAGE=aquest-backend:ci DATA_DIR="$compose_fixture" \
    docker compose -f docker-compose.yml -f docker-compose.bind.yml config --quiet
  cleanup_compose_fixture
  docker build --file Dockerfile --tag aquest-backend:ci .
}

case "$mode" in
  python)
    run_python_checks
    verify_mark python
    ;;
  frontend)
    run_frontend_checks
    verify_mark frontend
    ;;
  frontend-fast)
    run_frontend_fast_checks
    verify_mark frontend-fast
    ;;
  docker)
    run_docker_checks
    verify_mark docker
    ;;
  all)
    run_python_checks
    verify_mark python
    run_frontend_checks
    verify_mark frontend
    if [ "$skip_docker" -eq 0 ]; then
      run_docker_checks
      verify_mark docker
    else
      printf '%s\n' '已跳过 Docker 检查（显式指定 --skip-docker）'
    fi
    ;;
esac

printf '%s\n' '验证完成：所有选定检查均通过。'
