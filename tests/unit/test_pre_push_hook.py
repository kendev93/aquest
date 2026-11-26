"""pre-push 的盖章复用：推 annotated tag 不该把整套检查再跑一遍。

标签推的是 tag 对象自己的 OID，不是 commit 的 OID；盖章按 HEAD 的 tree 算，所以判断
「推的是不是当前内容」也必须先剥到 tree。早先直接比 OID，推送 release tag 时永远不
相等，刚推完分支又会重跑一次 Python 与前端检查。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _run_hook(repo: Path, ref_name: str, oid: str) -> None:
    subprocess.run(
        ["sh", ".githooks/pre-push", "origin", "git@example.com:repo"],
        cwd=repo,
        input=f"{ref_name} {oid} {ref_name} {'0' * 40}\n",
        text=True,
        check=True,
        capture_output=True,
        env=os.environ.copy(),
    )


def test_pre_push_reuses_stamp_for_annotated_tag(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")

    (repo / "file.txt").write_text("one")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-q", "-m", "one")
    _git(repo, "tag", "-a", "old", "-m", "old")

    (repo / "file.txt").write_text("two")
    _git(repo, "commit", "-q", "-am", "two")
    shutil.copytree(_REPO_ROOT / ".githooks", repo / ".githooks")
    (repo / "scripts").mkdir()
    shutil.copy(_REPO_ROOT / "scripts" / "verify_stamp.sh", repo / "scripts")
    # 真正的 verify_ci.sh 要跑几分钟；这里只记录调用并把该范围盖章。
    (repo / "scripts" / "verify_ci.sh").write_text(
        '#!/bin/sh\n. "$(dirname "$0")/verify_stamp.sh"\nverify_mark "$1"\n'
        'printf "%s\\n" "$1" >> "$(git rev-parse --git-dir)/ran.log"\n'
    )
    _git(repo, "add", ".githooks", "scripts")
    _git(repo, "commit", "-qm", "verification fixtures")
    _git(repo, "tag", "-a", "current", "-m", "current")
    subprocess.run(
        ["sh", "-c", ". scripts/verify_stamp.sh; verify_mark python; verify_mark frontend-fast"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )

    # annotated tag 的 OID 与 commit 不同，但指向 HEAD 的内容：两个范围都该跳过。
    _run_hook(repo, "refs/tags/current", _git(repo, "rev-parse", "current"))
    log = repo / ".git" / "ran.log"
    assert not log.exists()

    # 浏览器测试由 CI 单独执行，不使本地两个范围的盖章失效。
    e2e = repo / "web-ui" / "e2e" / "check.spec.ts"
    e2e.parent.mkdir(parents=True)
    e2e.write_text("// e2e")
    _git(repo, "add", "web-ui")
    _git(repo, "commit", "-qm", "e2e only")
    _run_hook(repo, "refs/heads/main", _git(repo, "rev-parse", "HEAD"))
    assert not log.exists()

    source = repo / "web-ui" / "src" / "app.ts"
    source.parent.mkdir()
    source.write_text("// frontend")
    _git(repo, "add", "web-ui")
    _git(repo, "commit", "-qm", "frontend only")
    _run_hook(repo, "refs/heads/main", _git(repo, "rev-parse", "HEAD"))
    assert log.read_text().split() == ["frontend-fast"]

    # 指向旧内容的标签仍要完整验证，不能因为「是个标签」就复用 HEAD 的章。
    _run_hook(repo, "refs/tags/old", _git(repo, "rev-parse", "old"))
    assert log.read_text().split() == ["frontend-fast", "python", "frontend-fast"]
