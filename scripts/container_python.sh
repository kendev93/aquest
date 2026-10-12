#!/usr/bin/env bash
#
# Run a throwaway Python script inside the backend container.
#
# The script is streamed in over stdin, executed, and the working directory is
# removed whether the run succeeds or fails. Nothing is left behind that a
# later cleanup step has to remember.
#
# Usage:
#   scripts/container_python.sh path/to/script.py [args...]
#
# Optional:
#   CONTAINER       container name  (default: aquest-backend-1)
#   COPY_SOURCES    space-separated package directories to ship alongside the
#                   script, relative to the repository root.
#
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
container="${CONTAINER:-aquest-backend-1}"

# `docker` is frequently not on PATH even when it is installed.
docker_bin="$(command -v docker 2>/dev/null || true)"
if [[ -z "$docker_bin" && -x /usr/local/bin/docker ]]; then
  docker_bin=/usr/local/bin/docker
fi
if [[ -z "$docker_bin" ]]; then
  echo "找不到 docker 可执行文件" >&2
  exit 127
fi

if [[ $# -lt 1 ]]; then
  echo "用法: scripts/container_python.sh <script.py> [args...]" >&2
  exit 2
fi

script="$1"
shift

if [[ ! -f "$script" ]]; then
  echo "找不到脚本: $script" >&2
  exit 2
fi

# A root-owned scratch dir, because files copied into the container land as
# root and the app user cannot remove them.
work="$("$docker_bin" exec -u root "$container" mktemp -d /tmp/selector-run.XXXXXX)"

cleanup() {
  "$docker_bin" exec -u root "$container" rm -rf "$work" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# COPYFILE_DISABLE keeps macOS tar from emitting AppleDouble metadata, which
# the container's tar would otherwise warn about on every entry.
for source_dir in ${COPY_SOURCES:-}; do
  COPYFILE_DISABLE=1 tar -cf - -C "$repo_root" "$source_dir" |
    "$docker_bin" exec -u root -i "$container" tar -xf - -C "$work" 2>/dev/null
done

"$docker_bin" exec -u root -i "$container" sh -c "cat > '$work/run.py'" <"$script"

set +e
# Both roots: `$work` for packages shipped at top level, `$work/aquest`
# for the usual `COPY_SOURCES=aquest/<pkg>` case, where the rest of
# `aquest` still resolves from the image's installed copy.
"$docker_bin" exec -u root -e PYTHONPATH="$work:$work/aquest" \
  "$container" python3 "$work/run.py" "$@"
status=$?
set -e

exit "$status"
