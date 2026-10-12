#!/bin/sh

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

git -C "$REPO_ROOT" config core.hooksPath .githooks
printf '%s\n' '已启用项目 Git Hooks：提交前静态检查（秒级），推送前 Python 全量测试。前端 E2E 与 Docker 检查由 CI 执行。'
