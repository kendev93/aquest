from __future__ import annotations

from pathlib import Path


def test_runtime_manifest_uses_lower_bounds_without_removed_online_dependency() -> None:
    manifest = Path(__file__).parents[2] / "requirements.lock"
    requirements = [
        line.strip()
        for line in manifest.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    assert requirements
    assert all("==" not in line for line in requirements)
    assert all("akshare" not in line.lower() for line in requirements)
    assert all("eastmoney" not in line.lower() for line in requirements)
    assert all(">=" in line for line in requirements)
