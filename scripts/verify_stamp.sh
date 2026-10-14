#!/bin/sh
# 验证盖章：记录「这一份代码已经验证过」，让 pre-push 不必每次都重跑全量测试。
#
# 为什么不看工作区：pre-push 推的是已提交的 commit，工作区里没提交的改动不会被推上去。
# 指纹取 HEAD 的 tree —— 源码、测试、依赖锁、脚本、hook 全都在 tree 里，改任何一处
# 都会让它变；tree 没变就说明要推的内容和上次验证通过的完全一致。解释器版本不在
# tree 里（换 Python/Node 会让结果不可比），所以单独进指纹。
#
# 强制重跑：AQUEST_VERIFY_FORCE=1 git push

set -eu

verify_fingerprint() {
  # $1 = 验证范围（python|frontend|docker）
  scope=$1
  {
    printf 'scope=%s\n' "$scope"
    printf 'tree=%s\n' "$(git rev-parse 'HEAD^{tree}')"
    python_bin=$(command -v python || command -v python3 || printf '')
    if [ -n "$python_bin" ]; then
      "$python_bin" -VV 2>/dev/null | head -1
    fi
    if command -v node >/dev/null 2>&1; then
      node -v
    fi
  } | digest
}

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
