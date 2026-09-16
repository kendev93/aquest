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
    assert "source: ${ETFQUEST_APP_DATA_DIR" in compose
    assert "indicator_lab_market" not in compose
    assert "SELECTOR_DISCOVERY_JOB_DIR" not in compose
    assert "SELECTOR_STRATEGY_WORKER_API_URL: http://backend:8123" in compose
    assert "SELECTOR_STRATEGY_WORKER_TOKEN_FILE: /run/secrets/strategy_worker_token" in compose
    assert "strategy_worker_token:" in compose
    assert "SELECTOR_DISCOVERY_WORKERS" in compose
    assert 'mem_limit: "${STRATEGY_WORKER_MEMORY_LIMIT:-0}"' in compose
    assert "healthcheck:" in compose
    assert 'test: ["CMD-SHELL", "kill -0 1"]' in compose
    assert "SELECTOR_DISCOVERY_WORKERS=auto" in env_example
    assert "SELECTOR_DISCOVERY_MEMORY_BUDGET_MB=auto" in env_example


def test_indicator_lab_start_scripts_auto_select_the_etfquest_database() -> None:
    shell_script = (PROJECT_ROOT / "scripts" / "start_indicator_lab.sh").read_text(encoding="utf-8")
    powershell_script = (PROJECT_ROOT / "scripts" / "start_indicator_lab.ps1").read_text(
        encoding="utf-8"
    )
    compose = (PROJECT_ROOT / "docker-compose.etfquest.yml").read_text(encoding="utf-8")

    assert "Library/Application Support/ETFQuest/database" in shell_script
    assert "CommonApplicationData" in powershell_script
    assert "SELECTOR_MARKET_DATA_SOURCE=etfquest" in shell_script
    assert 'SELECTOR_MARKET_DATA_SOURCE = "etfquest"' in powershell_script
    assert "SELECTOR_ETFQUEST_SECRET_KEY_PATH" in shell_script
    assert "ETFQUEST_APP_DATA_DIR" in powershell_script
    assert "ETFQUEST_APP_DATA_DIR" in shell_script
    assert "/run/etfquest/.secret.key" in compose
    assert compose.count("read_only: false") == 1
    assert compose.count("read_only: true") == 2


def test_membership_secret_key_is_readonly_and_backend_only() -> None:
    compose = (PROJECT_ROOT / "docker-compose.etfquest.yml").read_text(encoding="utf-8")
    backend, strategy_worker = compose.split("  strategy-worker:", maxsplit=1)

    assert "ETFQUEST_APP_DATA_DIR" in backend
    assert "target: /run/etfquest/.secret.key" in backend
    assert "create_host_path: false" in backend
    assert "target: /run/etfquest/.secret.key\n        read_only: true" in backend
    assert ".secret.key" not in strategy_worker


def test_indicator_lab_start_script_handles_macos_application_support_path(tmp_path: Path) -> None:
    home = tmp_path / "home"
    database_dir = home / "Library" / "Application Support" / "ETFQuest" / "database"
    database_dir.mkdir(parents=True)
    (database_dir / "market_data.db").touch()
    (database_dir.parent / ".secret.key").write_text("test-key", encoding="utf-8")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker_args = tmp_path / "docker-args.txt"
    fake_uname = fake_bin / "uname"
    fake_uname.write_text(
        "#!/bin/sh\nprintf '%s\\n' Darwin\n",
        encoding="utf-8",
    )
    fake_uname.chmod(0o755)

    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$DOCKER_ARGS_FILE"\n',
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    fake_openssl = fake_bin / "openssl"
    fake_openssl.write_text(
        "#!/bin/sh\nprintf '%s\\n' test-worker-token\n",
        encoding="utf-8",
    )
    fake_openssl.chmod(0o755)

    environment = os.environ.copy()
    environment.pop("ETFQUEST_MARKET_DATABASE_DIR", None)
    environment.pop("ETFQUEST_APP_DATA_DIR", None)
    environment["HOME"] = str(home)
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
    environment["DOCKER_ARGS_FILE"] = str(docker_args)

    result = subprocess.run(
        [str(PROJECT_ROOT / "scripts" / "start_indicator_lab.sh")],
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
    token_file = database_dir.parent / "indicator-lab" / ".strategy-worker-token"
    assert token_file.read_text(encoding="utf-8").strip() == "test-worker-token"
    assert token_file.stat().st_mode & 0o777 == 0o600


STANDALONE_COMPOSE = PROJECT_ROOT / "docker-compose.standalone.yml"
STANDALONE_BIND_COMPOSE = PROJECT_ROOT / "docker-compose.standalone-bind.yml"


def test_standalone_compose_does_not_depend_on_an_etfquest_install() -> None:
    compose = STANDALONE_COMPOSE.read_text(encoding="utf-8")

    assert "ETFQUEST_APP_DATA_DIR" not in compose
    assert "SELECTOR_ETFQUEST_DATABASE_PATH" not in compose
    assert "SELECTOR_MARKET_DATA_SOURCE" not in compose
    assert "/run/etfquest" not in compose
    assert "run/secrets" not in compose


def test_standalone_compose_defaults_to_a_named_volume() -> None:
    """默认必须是命名卷：绑定挂载会引入宿主 uid 与文件共享层的问题。"""

    compose = STANDALONE_COMPOSE.read_text(encoding="utf-8")

    assert compose.count("- indicator-lab-data:/data/market") == 2
    assert "type: bind" not in compose
    assert "\nvolumes:\n  indicator-lab-data:\n" in compose


def test_standalone_compose_generates_the_worker_token_on_first_boot() -> None:
    compose = STANDALONE_COMPOSE.read_text(encoding="utf-8")

    assert "SELECTOR_STRATEGY_WORKER_TOKEN_FILE: /data/market/.strategy-worker-token" in compose
    assert 'SELECTOR_STRATEGY_WORKER_TOKEN_AUTOGENERATE: "1"' in compose
    assert "SELECTOR_STRATEGY_WORKER_API_URL: http://backend:8123" in compose


def test_standalone_compose_does_not_retain_a_full_database_backup() -> None:
    """数据库有几 GB，默认再留一份整库备份会直接翻倍占用。"""

    compose = STANDALONE_COMPOSE.read_text(encoding="utf-8")

    assert 'SELECTOR_DB_BACKUP_RETENTION_DAYS: "${SELECTOR_DB_BACKUP_RETENTION_DAYS:-0}"' in compose


def test_bind_override_only_repoints_the_volume() -> None:
    """覆盖文件只改卷的落盘位置，服务定义不动，避免 volumes 列表的合并歧义。"""

    override = STANDALONE_BIND_COMPOSE.read_text(encoding="utf-8")

    assert "\nservices:" not in override
    assert "driver_opts:" in override
    assert "device: ${DATA_DIR:?请设置 DATA_DIR 为宿主目录}" in override
    assert "chown 10001:10001" in override, "必须写明绑定挂载的属主要求"


def test_standalone_image_tag_must_be_set_explicitly() -> None:
    compose = STANDALONE_COMPOSE.read_text(encoding="utf-8")

    assert compose.count("${INDICATOR_LAB_IMAGE:?") == 2
