#!/bin/sh

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

git -C "$REPO_ROOT" config core.hooksPath .githooks
printf '%s\n' '已启用项目 Git Hooks：提交前秒级静态检查；推送前按代码内容判断 Python 全量检查是否已验证过，只有变了才重跑（AQUEST_VERIFY_FORCE=1 可强制）。前端 E2E 与 Docker 检查由 CI 执行。'
