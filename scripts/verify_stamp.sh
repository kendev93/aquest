#!/bin/sh
# 验证盖章：记录「这一份代码已经验证过」，让 pre-push 不必每次都重跑全量测试。
#
# 按验证范围读取已提交的输入，其他模块的改动不会让本范围的盖章失效。
# 强制重跑：AQUEST_VERIFY_FORCE=1 git push

set -eu

verify_python_bin() {
  if [ -x .venv/bin/python ]; then
    printf '%s\n' "$PWD/.venv/bin/python"
  else
    command -v python || command -v python3
  fi
}

verify_inputs() {
  case "$1" in
    python)
      git ls-tree -r HEAD -- aquest tests pyproject.toml requirements.in requirements.txt \
        scripts .githooks .github/workflows/ci.yml data assets docker Dockerfile \
        docker-compose.yml docker-compose.bind.yml docker-compose.tdx.yml
      ;;
    frontend-fast)
      git ls-tree -r HEAD -- web-ui/src web-ui/public web-ui/index.html web-ui/package.json \
        web-ui/package-lock.json web-ui/tsconfig.json web-ui/tsconfig.app.json \
        web-ui/tsconfig.node.json web-ui/vite.config.ts web-ui/vitest.config.ts \
        scripts .githooks .github/workflows/ci.yml
      ;;
    *) git rev-parse 'HEAD^{tree}' ;;
  esac
}

verify_fingerprint() (
  {
    printf 'scope=%s\n' "$1"
    verify_inputs "$1"
    case "$1" in
      frontend-fast|frontend|docker)
        if command -v node >/dev/null 2>&1; then node -v; fi
        ;;
      python)
        if python_bin=$(verify_python_bin); then
          "$python_bin" -c 'import sys; print(sys.executable); print(sys.version)'
        fi
        ;;
    esac
  } | digest
)

digest() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum | cut -c1-32
  else
    shasum -a 256 | cut -c1-32
  fi
}

verify_stamp_file() {
  printf '%s/aquest-verify/%s.ok' "$(git rev-parse --git-dir)" "$1"
}

verify_is_fresh() {
  # 退出码 0 = 已验证过，可以跳过；非 0 = 需要重跑。
  file=$(verify_stamp_file "$1")
  [ "${AQUEST_VERIFY_FORCE:-0}" = "1" ] && return 1
  [ -f "$file" ] || return 1
  [ "$(cat "$file")" = "$(verify_fingerprint "$1")" ]
}

verify_mark() {
  # 测试运行在工作区，含未提交改动的结果不能给 HEAD 盖章。
  if [ -n "$(git status --porcelain --untracked-files=normal)" ]; then
    printf '工作区有未提交改动，本次不写入 %s 验证记录。\n' "$1"
    return 0
  fi
  file=$(verify_stamp_file "$1")
  mkdir -p "$(dirname "$file")"
  verify_fingerprint "$1" >"$file"
}

verify_describe() {
  file=$(verify_stamp_file "$1")
  if [ -f "$file" ]; then
    printf '已验证（stamp %s…）' "$(cut -c1-8 <"$file")"
  else
    printf '未验证'
  fi
}
