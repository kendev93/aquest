#!/bin/sh

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

database_dir=${ETFQUEST_MARKET_DATABASE_DIR:-}
if [ -z "$database_dir" ]; then
  case "$(uname -s)" in
    Darwin)
      database_dir=${HOME}/Library/Application Support/ETFQuest/database
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

database_file=$database_dir/market_data.db
if [ ! -f "$database_file" ]; then
  printf '未找到 ETFQuest 行情数据库：%s\n' "$database_file" >&2
  printf '%s\n' '请先安装并初始化最新版 ETFQuest，或设置 ETFQUEST_MARKET_DATABASE_DIR 后重试。' >&2
  exit 1
fi
if [ ! -r "$database_file" ]; then
  printf 'ETFQuest 行情数据库不可读：%s\n' "$database_file" >&2
  exit 1
fi

if [ "${1:-}" = "--protected" ]; then
  shift
  compose_mode=protected
else
  compose_mode=development
fi

export ETFQUEST_MARKET_DATABASE_DIR=$database_dir
export SELECTOR_MARKET_DATA_SOURCE=etfquest
export SELECTOR_RELEASE_MODE=1
export SELECTOR_ETFQUEST_DATABASE_PATH=/run/etfquest/database/market_data.db

cd "$REPO_ROOT"
if [ "$compose_mode" = "protected" ]; then
  docker compose \
    -f "$REPO_ROOT/docker-compose.yml" \
    -f "$REPO_ROOT/docker-compose.protected.yml" \
    config --quiet
  exec docker compose \
    -f "$REPO_ROOT/docker-compose.yml" \
    -f "$REPO_ROOT/docker-compose.protected.yml" \
    up --build -d "$@"
fi
docker compose \
  -f "$REPO_ROOT/docker-compose.yml" \
  -f "$REPO_ROOT/docker-compose.etfquest.yml" \
  config --quiet
exec docker compose \
  -f "$REPO_ROOT/docker-compose.yml" \
  -f "$REPO_ROOT/docker-compose.etfquest.yml" \
  up --build -d "$@"
