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
    assert compose.count('SELECTOR_DISCOVERY_WORKERS: "${SELECTOR_DISCOVERY_WORKERS:-1}"') == 2
    assert "- indicator-lab-data:/data/market" in compose
    assert "SELECTOR_DISCOVERY_JOB_DIR" not in compose
    assert "SELECTOR_STRATEGY_WORKER_API_URL: http://backend:8123" in compose
    assert "SELECTOR_STRATEGY_WORKER_TOKEN_FILE: /data/market/.strategy-worker-token" in compose
    assert "SELECTOR_DISCOVERY_WORKERS" in compose
    assert 'mem_limit: "${STRATEGY_WORKER_MEMORY_LIMIT:-0}"' in compose
    assert "healthcheck:" in compose
    assert 'test: ["CMD-SHELL", "kill -0 1"]' in compose
    assert "SELECTOR_DISCOVERY_WORKERS=1" in env_example
    assert "SELECTOR_DISCOVERY_MEMORY_BUDGET_MB=auto" in env_example
    assert (
        'SELECTOR_STRATEGY_POOL_MAX_WORKERS: "${SELECTOR_STRATEGY_POOL_MAX_WORKERS:-1}"' in compose
    )
    assert "SELECTOR_STRATEGY_POOL_MAX_WORKERS=1" in env_example


COMPOSE = PROJECT_ROOT / "docker-compose.yml"
BIND_COMPOSE = PROJECT_ROOT / "docker-compose.bind.yml"


def test_compose_has_no_external_datasource_dependency() -> None:
    compose = COMPOSE.read_text(encoding="utf-8")

    assert "SELECTOR_MARKET_DATA_SOURCE" not in compose
    assert "run/secrets" not in compose


def test_compose_defaults_to_a_named_volume() -> None:
    """默认必须是命名卷：绑定挂载会引入宿主 uid 与文件共享层的问题。"""

    compose = COMPOSE.read_text(encoding="utf-8")

    assert compose.count("- indicator-lab-data:/data/market") == 2
    assert "type: bind" not in compose
    assert "\nvolumes:\n  indicator-lab-data:\n" in compose


def test_compose_generates_the_worker_token_on_first_boot() -> None:
    compose = COMPOSE.read_text(encoding="utf-8")

    assert "SELECTOR_STRATEGY_WORKER_TOKEN_FILE: /data/market/.strategy-worker-token" in compose
    assert 'SELECTOR_STRATEGY_WORKER_TOKEN_AUTOGENERATE: "1"' in compose
    assert "SELECTOR_STRATEGY_WORKER_API_URL: http://backend:8123" in compose


def test_compose_does_not_retain_a_full_database_backup() -> None:
    """数据库有几 GB，默认再留一份整库备份会直接翻倍占用。"""

    compose = COMPOSE.read_text(encoding="utf-8")

    assert 'SELECTOR_DB_BACKUP_RETENTION_DAYS: "${SELECTOR_DB_BACKUP_RETENTION_DAYS:-0}"' in compose


def test_bind_override_only_repoints_the_volume() -> None:
    """覆盖文件只改卷的落盘位置，服务定义不动，避免 volumes 列表的合并歧义。"""

    override = BIND_COMPOSE.read_text(encoding="utf-8")

    assert "\nservices:" not in override
    assert "driver_opts:" in override
    assert "device: ${DATA_DIR:?请设置 DATA_DIR 为宿主目录}" in override
    assert "chown 10001:10001" in override, "必须写明绑定挂载的属主要求"


def test_image_tag_must_be_set_explicitly() -> None:
    compose = COMPOSE.read_text(encoding="utf-8")

    assert compose.count("${INDICATOR_LAB_IMAGE:?") == 2
