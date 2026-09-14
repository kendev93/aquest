import os
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_container_backend_uses_port_8123_end_to_end() -> None:
    compose = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    nginx = (PROJECT_ROOT / "docker" / "nginx.conf").read_text(encoding="utf-8")
    env_example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")

    assert '      - "8123"' in compose
    assert "127.0.0.1:${API_PORT:-8123}:8123" in compose
    assert "EXPOSE 8123" in dockerfile
    assert '--port", "8123"' in dockerfile
    assert "server backend:8123;" in nginx
    assert "API_PORT=8123" in env_example


def test_compose_defines_an_isolated_strategy_worker() -> None:
    compose = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    env_example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")

    assert "  strategy-worker:" in compose
    assert 'command: ["python", "-m", "selector_app.strategy_discovery.worker"]' in compose
    assert "SELECTOR_DATA_DIR: /data/market" in compose
    assert (
        compose.count(
            'SELECTOR_DISCOVERY_MEMORY_BUDGET_MB: "${SELECTOR_DISCOVERY_MEMORY_BUDGET_MB:-auto}"'
        )
        == 2
    )
    assert compose.count('SELECTOR_DISCOVERY_WORKERS: "${SELECTOR_DISCOVERY_WORKERS:-auto}"') == 2
    assert "source: market_data" in compose
    assert "SELECTOR_DISCOVERY_WORKERS" in compose
    assert 'mem_limit: "${STRATEGY_WORKER_MEMORY_LIMIT:-0}"' in compose
    assert "healthcheck:" in compose
    assert 'test: ["CMD-SHELL", "kill -0 1"]' in compose
    assert "SELECTOR_DISCOVERY_WORKERS=auto" in env_example
    assert "SELECTOR_DISCOVERY_MEMORY_BUDGET_MB=auto" in env_example


def test_shared_start_scripts_auto_select_the_etfquest_database() -> None:
    shell_script = (PROJECT_ROOT / "scripts" / "start_shared.sh").read_text(encoding="utf-8")
    powershell_script = (PROJECT_ROOT / "scripts" / "start_shared.ps1").read_text(encoding="utf-8")
    compose = (PROJECT_ROOT / "docker-compose.etfquest.yml").read_text(encoding="utf-8")

    assert "Library/Application Support/ETFQuest/database" in shell_script
    assert "CommonApplicationData" in powershell_script
    assert "SELECTOR_MARKET_DATA_SOURCE=etfquest" in shell_script
    assert 'SELECTOR_MARKET_DATA_SOURCE = "etfquest"' in powershell_script
    assert compose.count("read_only: false") == 1
    assert compose.count("read_only: true") == 1


def test_shared_start_script_handles_macos_application_support_path(tmp_path: Path) -> None:
    home = tmp_path / "home"
    database_dir = home / "Library" / "Application Support" / "ETFQuest" / "database"
    database_dir.mkdir(parents=True)
    (database_dir / "market_data.db").touch()

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker_args = tmp_path / "docker-args.txt"
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$DOCKER_ARGS_FILE"\n',
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    environment = os.environ.copy()
    environment.pop("ETFQUEST_MARKET_DATABASE_DIR", None)
    environment["HOME"] = str(home)
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
    environment["DOCKER_ARGS_FILE"] = str(docker_args)

    result = subprocess.run(
        [str(PROJECT_ROOT / "scripts" / "start_shared.sh")],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    calls = docker_args.read_text(encoding="utf-8")
    assert "docker-compose.etfquest.yml" in calls
    assert "up --build -d" in calls
