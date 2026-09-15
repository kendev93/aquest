from pathlib import Path

import pytest

from scripts.build_nuitka import BuildConfig, build_nuitka_command
from scripts.nuitka_entrypoint import split_runtime_args
from scripts.verify_nuitka_bundle import verify_bundle

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_nuitka_command_builds_one_shared_standalone_bundle(tmp_path: Path) -> None:
    config = BuildConfig(
        output_dir=tmp_path / "release",
        cache_dir=tmp_path / "cache",
        jobs=4,
        lto="yes",
        reproducible="yes",
    )
    native_library = (tmp_path / "lib_lightgbm.so", "lightgbm/lib/lib_lightgbm.so")

    command = build_nuitka_command(config, native_library=native_library)

    assert "--mode=standalone" in command
    assert "--onefile" not in command
    assert "--include-package=selector_app" in command
    assert "--include-package=cryptography" in command
    assert "--include-package=akshare" not in command
    assert "--include-package=duckdb" in command
    assert "--include-package=numpy" in command
    assert "--include-package=pandas" in command
    assert "--include-package=lightgbm" in command
    assert "--include-package=optuna" in command
    assert (
        "--include-data-files="
        + str(tmp_path / "lib_lightgbm.so")
        + "=lightgbm/lib/lib_lightgbm.so"
        in command
    )
    assert "--nofollow-import-to=pytest" in command
    assert "--nofollow-import-to=setuptools" in command
    assert "--jobs=4" in command
    assert "--lto=yes" in command
    assert "--reproducible=yes" in command
    assert "--output-filename=indicator-lab" in command
    assert "--update-check=never" in command
    assert command[-1].endswith("scripts/nuitka_entrypoint.py")


def test_release_entrypoint_keeps_server_compatibility_and_routes_worker() -> None:
    assert split_runtime_args(()) == ("server", ())
    assert split_runtime_args(("--port", "9000")) == ("server", ("--port", "9000"))
    assert split_runtime_args(("server", "--host", "0.0.0.0")) == (
        "server",
        ("--host", "0.0.0.0"),
    )
    assert split_runtime_args(("worker",)) == ("worker", ())


def test_protected_bundle_rejects_python_source_and_bytecode(tmp_path: Path) -> None:
    bundle = tmp_path / "indicator-lab.dist"
    bundle.mkdir()
    (bundle / "indicator-lab").write_bytes(b"compiled")
    verify_bundle(bundle)

    (bundle / "selector_app.py").write_text("secret", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Python source or bytecode"):
        verify_bundle(bundle)


def test_protected_docker_runtime_only_copies_compiled_bundle() -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile.protected").read_text(encoding="utf-8")
    runtime_stage = dockerfile.split("FROM python:3.12-slim AS runtime", maxsplit=1)[1]
    compose = (PROJECT_ROOT / "docker-compose.protected.yml").read_text(encoding="utf-8")

    assert "COPY selector_app" in dockerfile
    assert "COPY --from=builder /build/release/indicator-lab.dist" in runtime_stage
    assert "COPY selector_app" not in runtime_stage
    assert "Dockerfile.protected" in compose
    assert 'command: ["server", "--host", "0.0.0.0", "--port", "8123"]' in compose
    assert 'command: ["worker"]' in compose
