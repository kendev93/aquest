#!/bin/sh

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

usage() {
  cat <<'EOF'
用法: sh scripts/verify_ci.sh [all|python|frontend|docker|protected] [--skip-docker] [--install-playwright]

依赖需要提前安装；脚本只执行检查，不自动安装 Python/npm 依赖。
默认 all 会执行 Python、前端和 Docker 全部检查。
--skip-docker 只对 all 生效，用于本地快速检查。
--install-playwright 在前端检查前执行与 CI 相同的浏览器安装命令。
EOF
}

mode=all
skip_docker=0
install_playwright=0
compose_fixture=""

for argument in "$@"; do
  case "$argument" in
    all|python|frontend|docker|protected)
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

cleanup_compose_fixture() {
  if [ -n "$compose_fixture" ] && [ -d "$compose_fixture" ]; then
    rm -r "$compose_fixture"
  fi
  compose_fixture=""
}

prepare_compose_fixture() {
  compose_fixture=$(mktemp -d "${TMPDIR:-/tmp}/indicator-lab-compose.XXXXXX")
  mkdir -p "$compose_fixture/database" "$compose_fixture/indicator-lab"
  : > "$compose_fixture/database/market_data.db"
  : > "$compose_fixture/.secret.key"
  printf '%s\n' 'ci-test-worker-token' > "$compose_fixture/indicator-lab/.strategy-worker-token"
}

trap cleanup_compose_fixture EXIT

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
  PYTHONPATH=. "$python_bin" -m pytest --cov=selector_app --cov-report=term-missing
  ruff format --check selector_app tests
  ruff check selector_app tests
  mypy selector_app
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

run_docker_checks() {
  printf '%s\n' '验证 Docker：Compose 配置、后端镜像、前端镜像'
  prepare_compose_fixture
  ETFQUEST_APP_DATA_DIR="$compose_fixture" \
    docker compose -f docker-compose.yml config --quiet
  ETFQUEST_APP_DATA_DIR="$compose_fixture" \
    ETFQUEST_MARKET_DATABASE_DIR="$compose_fixture/database" \
    docker compose -f docker-compose.yml -f docker-compose.etfquest.yml config --quiet
  cleanup_compose_fixture
  docker build --file Dockerfile --tag indicator-lab-backend:ci .
  docker build --file Dockerfile.frontend --tag indicator-lab-frontend:ci .
}

run_protected_docker_checks() {
  printf '%s\n' '验证保护版 Docker：Nuitka 编译、产物源码扫描、最终镜像构建'
  prepare_compose_fixture
  ETFQUEST_MARKET_DATABASE_DIR="$compose_fixture/database" \
    ETFQUEST_APP_DATA_DIR="$compose_fixture" \
    docker compose -f docker-compose.yml -f docker-compose.protected.yml config --quiet
  cleanup_compose_fixture
  docker build --file Dockerfile.protected --tag indicator-lab-protected:ci .
}

case "$mode" in
  python)
    run_python_checks
    ;;
  frontend)
    run_frontend_checks
    ;;
  docker)
    run_docker_checks
    ;;
  protected)
    run_protected_docker_checks
    ;;
  all)
    run_python_checks
    run_frontend_checks
    if [ "$skip_docker" -eq 0 ]; then
      run_docker_checks
    else
      printf '%s\n' '已跳过 Docker 检查（显式指定 --skip-docker）'
    fi
    ;;
esac

printf '%s\n' '验证完成：所有选定检查均通过。'
