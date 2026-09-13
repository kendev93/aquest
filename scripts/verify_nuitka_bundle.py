"""Verify that a Nuitka release bundle does not contain Python source or bytecode."""

from __future__ import annotations

import argparse
from pathlib import Path

_FORBIDDEN_SUFFIXES = frozenset({".py", ".pyc", ".pyo"})


def _forbidden_files(bundle_dir: Path) -> tuple[Path, ...]:
    return tuple(
        sorted(
            path
            for path in bundle_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in _FORBIDDEN_SUFFIXES
        )
    )


def verify_bundle(bundle_dir: Path) -> None:
    """Fail closed when a compiled release still contains Python code artifacts."""

    if not bundle_dir.is_dir():
        raise RuntimeError(f"Nuitka standalone 产物目录不存在: {bundle_dir}")
    forbidden = _forbidden_files(bundle_dir)
    if forbidden:
        preview = ", ".join(str(path.relative_to(bundle_dir)) for path in forbidden[:10])
        suffix = " ..." if len(forbidden) > 10 else ""
        raise RuntimeError(
            f"Python source or bytecode remains in protected bundle: {preview}{suffix}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查 Nuitka 保护版产物是否包含 Python 源码")
    parser.add_argument("bundle_dir", type=Path)
    args = parser.parse_args(argv)
    verify_bundle(args.bundle_dir)
    print(f"保护版产物检查通过: {args.bundle_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
