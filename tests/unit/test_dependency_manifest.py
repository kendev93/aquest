from __future__ import annotations

from pathlib import Path


def test_runtime_manifest_uses_lower_bounds_and_includes_akshare() -> None:
    manifest = Path(__file__).parents[2] / "requirements.lock"
    requirements = [
        line.strip()
        for line in manifest.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    assert requirements
    assert all("==" not in line for line in requirements)
    assert "akshare>=1.18.94" in requirements
    assert all(">=" in line for line in requirements)
