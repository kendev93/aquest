#!/bin/sh
# 启动A股策略研究。
#
#   sh scripts/start.sh                 重建镜像并启动（默认）
#   sh scripts/start.sh --build-only    只构建镜像，不启动
#   sh scripts/start.sh --no-build      不重建，直接启动
#
# 配置写在 .env 里（compose 会自动读取）：
#   AQUEST_IMAGE           必填，backend 与 strategy-worker 的镜像标签
#   DATA_DIR                      可选；设置后自动叠加绑定挂载覆盖文件，
#                                 数据落到该宿主目录。该目录必须允许容器内
#                                 appuser(uid 10001) 写入，否则容器起不来。
#
# 默认带 --build：改完代码直接跑这个脚本就会重建镜像。不加的话 compose 会
# 继续用旧镜像，是个很容易踩的坑。

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

usage() {
  # 打印 shebang 之后连续的注释块，头部注释增删都不用改这里。
  awk 'NR > 1 { if (/^#/) { sub(/^# ?/, ""); print; next } exit }' "$0"
}

build_only=0
no_build=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --build-only)
      build_only=1
      shift
      ;;
    --no-build)
      no_build=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      break
      ;;
  esac
done

# .env 由 compose 自己读取，但这里也要判断 DATA_DIR 是否需要绑定挂载覆盖文件。
if [ -z "${DATA_DIR:-}" ] && [ -f "$REPO_ROOT/.env" ]; then
  DATA_DIR=$(sed -n 's/^[[:space:]]*DATA_DIR[[:space:]]*=[[:space:]]*//p' "$REPO_ROOT/.env" | tail -1)
fi

compose() {
  if [ -n "${DATA_DIR:-}" ]; then
    docker compose \
      -f "$REPO_ROOT/docker-compose.yml" \
      -f "$REPO_ROOT/docker-compose.bind.yml" \
      "$@"
    return
  fi
  docker compose -f "$REPO_ROOT/docker-compose.yml" "$@"
}

cd "$REPO_ROOT"

if [ -n "${DATA_DIR:-}" ]; then
  printf '数据目录：%s（绑定挂载）\n' "$DATA_DIR"
else
  printf '%s\n' '数据目录：Docker 命名卷 aquest_aquest-data'
fi

# 先校验再动手：缺 AQUEST_IMAGE、变量拼错、覆盖文件冲突都会在这里报出来。
printf '%s\n' '校验 Compose 配置…'
compose config --quiet

if [ "$build_only" -eq 1 ]; then
  printf '%s\n' '构建镜像（不启动）…'
  compose build
  printf '%s\n' '构建完成。'
  exit 0
fi

if [ "$no_build" -eq 1 ]; then
  printf '%s\n' '跳过构建，直接启动…'
  compose up -d "$@"
else
  printf '%s\n' '重建镜像并启动…'
  compose up --build -d "$@"
fi

printf '\n%s\n' '当前状态：'
compose ps
printf '\n浏览器打开 http://127.0.0.1:%s/\n' "${WEB_PORT:-5173}"
