"""Build the server and strategy worker into one protected Nuitka bundle."""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = PROJECT_ROOT / "scripts" / "nuitka_entrypoint.py"
VERIFY_SCRIPT = PROJECT_ROOT / "scripts" / "verify_nuitka_bundle.py"
BINARY_NAME = "indicator-lab"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "dist" / "protected"
DEFAULT_CACHE_DIR = PROJECT_ROOT / "dist" / ".nuitka-cache"

NuitkaToggle = Literal["auto", "yes", "no"]


@dataclass(frozen=True)
class BuildConfig:
    """Inputs shared by local, CI, and protected Docker builds."""

    output_dir: Path
    cache_dir: Path
    jobs: int = 4
    lto: NuitkaToggle = "auto"
    reproducible: NuitkaToggle = "auto"
    clean: bool = False


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("必须是正整数") from error
    if parsed < 1:
        raise argparse.ArgumentTypeError("必须是正整数")
    return parsed


def _env_toggle(name: str, default: NuitkaToggle) -> NuitkaToggle:
    value = str(os.environ.get(name, default) or "").strip().lower()
    if value not in {"auto", "yes", "no"}:
        raise ValueError(f"{name} 必须是 auto、yes 或 no，当前值为 {value!r}")
    return value  # type: ignore[return-value]


def default_jobs() -> int:
    """Use a conservative compiler default; CI/users can raise it explicitly."""

    configured = str(os.environ.get("NUITKA_JOBS", "4") or "4").strip()
    return _positive_int(configured)


def find_lightgbm_native_library() -> tuple[Path, str]:
    """Locate the ctypes-loaded LightGBM library and its package-relative target."""

    spec = importlib.util.find_spec("lightgbm")
    locations = tuple(spec.submodule_search_locations or ()) if spec is not None else ()
    if not locations:
        raise RuntimeError("当前构建环境找不到 lightgbm 包")

    package_dir = Path(locations[0])
    if sys.platform.startswith("win"):
        suffix = ".dll"
    elif sys.platform == "darwin":
        suffix = ".dylib"
    else:
        suffix = ".so"
    library_name = f"lib_lightgbm{suffix}"
    candidates = (
        (Path("lib") / library_name, f"lightgbm/lib/{library_name}"),
        (Path("bin") / library_name, f"lightgbm/bin/{library_name}"),
        (Path(library_name), f"lightgbm/{library_name}"),
    )
    for relative_path, target_path in candidates:
        source = package_dir / relative_path
        if source.is_file():
            return source, target_path
    raise RuntimeError(f"lightgbm 原生库不存在: {package_dir}")


def build_nuitka_command(
    config: BuildConfig,
    *,
    native_library: tuple[Path, str] | None = None,
) -> list[str]:
    """Construct the audited command used by all protected builds."""

    command = [
        sys.executable,
        "-m",
        "nuitka",
        "--mode=standalone",
        f"--output-dir={config.output_dir}",
        f"--output-filename={BINARY_NAME}",
        "--include-package=selector_app",
        "--include-package=duckdb",
        "--include-package=numpy",
        "--include-package=pandas",
        "--include-package=lightgbm",
        "--include-package=optuna",
        "--include-package=scipy",
        "--include-package=narwhals",
        "--noinclude-pytest-mode=nofollow",
        "--noinclude-setuptools-mode=nofollow",
        "--nofollow-import-to=tests",
        "--nofollow-import-to=pytest",
        "--nofollow-import-to=_pytest",
        "--nofollow-import-to=IPython",
        "--nofollow-import-to=setuptools",
        "--nofollow-import-to=pip",
        "--nofollow-import-to=pandas.tests",
        "--nofollow-import-to=pandas._testing",
        "--nofollow-import-to=numpy.tests",
        "--nofollow-import-to=matplotlib",
        "--nofollow-import-to=sklearn",
        "--nofollow-import-to=tkinter",
        "--nofollow-import-to=turtle",
        "--nofollow-import-to=doctest",
        "--nofollow-import-to=unittest.mock",
        "--nofollow-import-to=xmlrpc",
        "--nofollow-import-to=distutils",
        "--nofollow-import-to=lib2to3",
        "--nofollow-import-to=pdb",
        "--nofollow-import-to=cProfile",
        "--nofollow-import-to=profile",
        "--python-flag=no_docstrings",
        f"--jobs={config.jobs}",
        f"--lto={config.lto}",
        f"--reproducible={config.reproducible}",
        f"--report={config.output_dir / f'{BINARY_NAME}-nuitka-report.xml'}",
        "--assume-yes-for-downloads",
        "--update-check=never",
    ]
    if sys.platform.startswith("win"):
        command.append("--include-windows-runtime-dlls=auto")
    if native_library is not None:
        source, target = native_library
        command.append(f"--include-data-files={source}={target}")
    command.append(str(ENTRYPOINT))
    return command


def _clean_output(output_dir: Path) -> None:
    """Remove only known artifacts from the dedicated protected output directory."""

    if not output_dir.is_dir():
        return
    known_prefixes = (BINARY_NAME, f"{BINARY_NAME}-nuitka-report")
    for child in output_dir.iterdir():
        if not child.name.startswith(known_prefixes):
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def _check_nuitka(environment: dict[str, str]) -> None:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "nuitka", "--update-check=never", "--version"],
            check=True,
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise RuntimeError(
            f"当前 Python 环境未能运行 Nuitka，请先安装 requirements-build.txt: {error}"
        ) from error
    version = (result.stdout or result.stderr).strip().splitlines()
    print(f"Nuitka: {version[0] if version else 'available'}")


def build(config: BuildConfig) -> Path:
    """Compile, verify, and return the standalone bundle directory."""

    if not ENTRYPOINT.is_file():
        raise RuntimeError(f"Nuitka 入口不存在: {ENTRYPOINT}")
    config.output_dir.mkdir(parents=True, exist_ok=True)
    config.cache_dir.mkdir(parents=True, exist_ok=True)
    if config.clean:
        _clean_output(config.output_dir)

    environment = os.environ.copy()
    environment["NUITKA_CACHE_DIR"] = str(config.cache_dir)
    _check_nuitka(environment)
    native_library = find_lightgbm_native_library()
    command = build_nuitka_command(config, native_library=native_library)
    print("开始构建保护版 standalone bundle")
    subprocess.run(command, check=True, cwd=PROJECT_ROOT, env=environment)

    bundle_dir = config.output_dir / f"{BINARY_NAME}.dist"
    subprocess.run(
        [sys.executable, str(VERIFY_SCRIPT), str(bundle_dir)],
        check=True,
        cwd=PROJECT_ROOT,
        env=environment,
    )
    return bundle_dir


def parse_args(argv: list[str] | None = None) -> BuildConfig:
    default_cache = Path(os.environ.get("NUITKA_CACHE_DIR", DEFAULT_CACHE_DIR))
    parser = argparse.ArgumentParser(description="构建 Indicator Lab 的 Nuitka 保护版产物")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--cache-dir", type=Path, default=default_cache)
    parser.add_argument("--jobs", type=_positive_int, default=default_jobs())
    parser.add_argument(
        "--lto", choices=("auto", "yes", "no"), default=_env_toggle("NUITKA_LTO", "auto")
    )
    parser.add_argument(
        "--reproducible",
        choices=("auto", "yes", "no"),
        default=_env_toggle("NUITKA_REPRODUCIBLE", "auto"),
    )
    parser.add_argument("--clean", action="store_true")
    args = parser.parse_args(argv)
    return BuildConfig(
        output_dir=args.output_dir.expanduser().resolve(),
        cache_dir=args.cache_dir.expanduser().resolve(),
        jobs=args.jobs,
        lto=args.lto,
        reproducible=args.reproducible,
        clean=args.clean,
    )


def main(argv: list[str] | None = None) -> int:
    build(parse_args(argv))
    print("保护版构建完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
