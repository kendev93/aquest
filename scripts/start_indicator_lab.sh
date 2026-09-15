#!/bin/sh
# Start Indicator Lab with ETFQuest market data and host-local application storage.

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

database_dir=${ETFQUEST_MARKET_DATABASE_DIR:-}
if [ -z "$database_dir" ]; then
    case "$(uname -s)" in
    Darwin)
      database_dir="${HOME}/Library/Application Support/ETFQuest/database"
      ;;
    Linux)
      database_dir=${XDG_DATA_HOME:-${HOME}/.local/share}/ETFQuest/database
      ;;
    *)
      printf '%s\n' '无法自动识别当前系统，请设置 ETFQUEST_MARKET_DATABASE_DIR。' >&2
      exit 2
      ;;
  esac
fi

database_file="${database_dir}/market_data.db"
if [ ! -f "$database_file" ]; then
  printf '未找到 ETFQuest 行情数据库：%s\n' "$database_file" >&2
  printf '%s\n' '请先安装并初始化最新版 ETFQuest，或设置 ETFQUEST_MARKET_DATABASE_DIR 后重试。' >&2
  exit 1
fi
if [ ! -r "$database_file" ]; then
  printf 'ETFQuest 行情数据库不可读：%s\n' "$database_file" >&2
  exit 1
fi

app_data_dir=${ETFQUEST_APP_DATA_DIR:-$(dirname "$database_dir")}
secret_key_file="${app_data_dir}/.secret.key"
if [ ! -f "$secret_key_file" ]; then
  printf '未找到 ETFQuest 授权快照密钥：%s\n' "$secret_key_file" >&2
  printf '%s\n' '请确认这是 ETFQuest 应用数据目录，且该目录包含 .secret.key。' >&2
  exit 1
fi
if [ ! -r "$secret_key_file" ]; then
  printf 'ETFQuest 授权快照密钥不可读：%s\n' "$secret_key_file" >&2
  exit 1
fi

worker_data_dir="${app_data_dir}/indicator-lab"
worker_token_file="${worker_data_dir}/.strategy-worker-token"
mkdir -p "$worker_data_dir"
chmod 700 "$worker_data_dir"
if [ ! -s "$worker_token_file" ]; then
  worker_token_tmp=$(mktemp "${worker_data_dir}/.strategy-worker-token.XXXXXX")
  if ! openssl rand -hex 32 > "$worker_token_tmp"; then
    rm -f "$worker_token_tmp"
    printf '%s\n' '无法生成策略 worker 认证密钥，请检查 openssl。' >&2
    exit 1
  fi
  chmod 600 "$worker_token_tmp"
  mv "$worker_token_tmp" "$worker_token_file"
else
  chmod 600 "$worker_token_file"
fi

compose_mode=development
no_build=0
build_only=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --protected)
      compose_mode=protected
      shift
      ;;
    --no-build)
      no_build=1
      shift
      ;;
    --build-only)
      build_only=1
      shift
      ;;
    *)
      break
      ;;
  esac
done

if [ "$no_build" -eq 1 ]; then
  build_switch=--no-build
else
  build_switch=--build
fi

export ETFQUEST_MARKET_DATABASE_DIR="$database_dir"
export ETFQUEST_APP_DATA_DIR="$app_data_dir"
export ETFQUEST_MARKET_UID="${ETFQUEST_MARKET_UID:-$(id -u)}"
export ETFQUEST_MARKET_GID="${ETFQUEST_MARKET_GID:-$(id -g)}"
export SELECTOR_MARKET_DATA_SOURCE=etfquest
export SELECTOR_RELEASE_MODE=1
export SELECTOR_ETFQUEST_DATABASE_PATH=/run/etfquest/database/market_data.db
export SELECTOR_ETFQUEST_SECRET_KEY_PATH=/run/etfquest/.secret.key

cd "$REPO_ROOT"
if [ "$compose_mode" = "protected" ]; then
  docker compose \
    -f "$REPO_ROOT/docker-compose.yml" \
    -f "$REPO_ROOT/docker-compose.protected.yml" \
    config --quiet
  if [ "$build_only" -eq 1 ]; then
    exec docker compose \
      -f "$REPO_ROOT/docker-compose.yml" \
      -f "$REPO_ROOT/docker-compose.protected.yml" \
      build backend strategy-worker
  fi
  exec docker compose \
    -f "$REPO_ROOT/docker-compose.yml" \
    -f "$REPO_ROOT/docker-compose.protected.yml" \
    up "$build_switch" -d "$@"
fi
docker compose \
  -f "$REPO_ROOT/docker-compose.yml" \
  -f "$REPO_ROOT/docker-compose.etfquest.yml" \
  config --quiet
if [ "$build_only" -eq 1 ]; then
  exec docker compose \
    -f "$REPO_ROOT/docker-compose.yml" \
    -f "$REPO_ROOT/docker-compose.etfquest.yml" \
    build backend strategy-worker
fi
exec docker compose \
  -f "$REPO_ROOT/docker-compose.yml" \
  -f "$REPO_ROOT/docker-compose.etfquest.yml" \
  up "$build_switch" -d "$@"
