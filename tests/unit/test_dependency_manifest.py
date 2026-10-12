from __future__ import annotations

from pathlib import Path


def test_runtime_lock_is_exact_and_excludes_removed_online_dependency() -> None:
    manifest = Path(__file__).parents[2] / "requirements.txt"
    lines = [
        line.strip()
        for line in manifest.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith(("#", "--hash="))
    ]
    requirements = [line.rstrip("\\").split(";", maxsplit=1)[0].strip() for line in lines]

    assert requirements
    assert all("==" in line for line in requirements)
    assert "--hash=sha256:" in manifest.read_text(encoding="utf-8")
    assert all("akshare" not in line.lower() for line in requirements)
    assert all("eastmoney" not in line.lower() for line in requirements)


def test_multipart_is_declared_because_the_import_endpoint_uploads_a_file() -> None:
    """回归：数据导入端点用 UploadFile，缺 python-multipart 时容器启动即崩。

    本地开发环境通常已经装了它，所以这个缺失只会在正式镜像里暴露——
    也就是部署时才炸。这里直接对着清单断言。
    """

    root = Path(__file__).parents[2]
    manifest = (root / "requirements.txt").read_text(encoding="utf-8").lower()
    project = (root / "pyproject.toml").read_text(encoding="utf-8").lower()

    assert "python-multipart" in manifest
    assert "python-multipart" in project


def test_every_fastapi_file_upload_route_has_its_dependency_declared() -> None:
    """凡是代码里出现 ``UploadFile``，运行依赖里就必须有 python-multipart。"""

    root = Path(__file__).parents[2]
    users = [
        path
        for path in (root / "aquest").rglob("*.py")
        if "UploadFile" in path.read_text(encoding="utf-8")
    ]

    if not users:
        return
    manifest = (root / "requirements.txt").read_text(encoding="utf-8").lower()
    assert "python-multipart" in manifest, f"以下文件需要 python-multipart: {users}"
