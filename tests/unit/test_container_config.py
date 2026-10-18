import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_backend_healthcheck_tolerates_a_startup_compaction() -> None:
    """启动时的一次全库压缩不能被健康检查判成「起不来」。

    lifespan 在绑定端口之前同步跑 `maintenance.run_if_needed()`：交换数据文件必须在还没有
    读取者时做。本轮实测（2026-10-18，昨天「一键修复」删并重写 113 万根之后触发）4.69GB 压到
    3.06GB 用了约 6 分钟，但共享卷慢的场景能到几十分钟，所以宽限按上限留到 60 分钟。旧配置
    start-period=20s + retries=3 约 110 秒就判 unhealthy，worker 因 `depends_on:
    service_healthy` 一直卡在 Created，看上去像整站挂了。
    """

    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    lines = dockerfile.splitlines()
    start = next(index for index, item in enumerate(lines) if item.startswith("HEALTHCHECK"))
    # 指令用反斜杠续行，真正的探测命令在下一行，要整块看。
    block = " ".join(lines[start : start + 2])
    flags = dict(re.findall(r"--([a-z-]+)=(\d+)s?", block))

    # 宽限期要盖得住一次最慢的全库压缩（实测 45 分钟）并留余量。
    assert int(flags["start-period"]) >= 3600
    # 过了宽限期必须恢复灵敏：真坏掉的启动不能被继续说成“还在启动”，取 3 次连败 × 30 秒。
    assert int(flags["retries"]) * int(flags["interval"]) <= 180
    # 健康检查探测的是 readiness（/healthz 经 nginx 打到应用），不是进程活着而已。
    assert "/healthz" in block


def test_container_backend_uses_port_8123_end_to_end() -> None:
    compose = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    supervisord = (PROJECT_ROOT / "docker" / "supervisord.conf").read_text(encoding="utf-8")
    nginx = (PROJECT_ROOT / "docker" / "nginx.conf").read_text(encoding="utf-8")
    env_example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")

    assert '      - "8123"' in compose
    assert "127.0.0.1:${API_PORT:-8123}:8123" in compose
    assert "EXPOSE 8123" in dockerfile
    # 单镜像：supervisord 托管 uvicorn 与 nginx，uvicorn 只监听容器内 8123。
    assert 'CMD ["supervisord"' in dockerfile
    assert "uvicorn aquest.web.app:app --host 0.0.0.0 --port 8123" in supervisord
    # nginx 与 uvicorn 同容器，upstream 指向本机回环而不是其他服务名。
    assert "server 127.0.0.1:8123;" in nginx
    assert "server backend:8123;" not in nginx
    assert "API_PORT=8123" in env_example


def test_compose_defines_an_isolated_strategy_worker() -> None:
    compose = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    env_example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")

    assert "  strategy-worker:" in compose
    assert 'command: ["python", "-m", "aquest.strategy_discovery.worker"]' in compose
    assert "AQUEST_DATA_DIR: /data/market" in compose
    assert (
        compose.count(
            'AQUEST_DISCOVERY_MEMORY_BUDGET_MB: "${AQUEST_DISCOVERY_MEMORY_BUDGET_MB:-auto}"'
        )
        == 2
    )
    assert compose.count('AQUEST_DISCOVERY_WORKERS: "${AQUEST_DISCOVERY_WORKERS:-1}"') == 2
    assert "- aquest-data:/data/market" in compose
    assert "AQUEST_DISCOVERY_JOB_DIR" not in compose
    assert "AQUEST_STRATEGY_WORKER_API_URL: http://backend:8123" in compose
    assert "AQUEST_STRATEGY_WORKER_TOKEN_FILE: /data/market/.strategy-worker-token" in compose
    assert "AQUEST_DISCOVERY_WORKERS" in compose
    assert 'mem_limit: "${STRATEGY_WORKER_MEMORY_LIMIT:-0}"' in compose
    assert "healthcheck:" in compose
    assert 'test: ["CMD-SHELL", "kill -0 1"]' in compose
    assert "AQUEST_DISCOVERY_WORKERS=1" in env_example
    assert "AQUEST_DISCOVERY_MEMORY_BUDGET_MB=auto" in env_example
    assert 'AQUEST_STRATEGY_POOL_MAX_WORKERS: "${AQUEST_STRATEGY_POOL_MAX_WORKERS:-1}"' in compose
    assert "AQUEST_STRATEGY_POOL_MAX_WORKERS=1" in env_example


COMPOSE = PROJECT_ROOT / "docker-compose.yml"
BIND_COMPOSE = PROJECT_ROOT / "docker-compose.bind.yml"


def test_compose_has_no_external_datasource_dependency() -> None:
    compose = COMPOSE.read_text(encoding="utf-8")

    assert "AQUEST_MARKET_DATA_SOURCE" not in compose
    assert "run/secrets" not in compose


def test_compose_defaults_to_a_named_volume() -> None:
    """默认必须是命名卷：绑定挂载会引入宿主 uid 与文件共享层的问题。"""

    compose = COMPOSE.read_text(encoding="utf-8")

    assert compose.count("- aquest-data:/data/market") == 2
    assert "type: bind" not in compose
    assert "\nvolumes:\n  aquest-data:\n" in compose


def test_compose_generates_the_worker_token_on_first_boot() -> None:
    compose = COMPOSE.read_text(encoding="utf-8")

    assert "AQUEST_STRATEGY_WORKER_TOKEN_FILE: /data/market/.strategy-worker-token" in compose
    assert 'AQUEST_STRATEGY_WORKER_TOKEN_AUTOGENERATE: "1"' in compose
    assert "AQUEST_STRATEGY_WORKER_API_URL: http://backend:8123" in compose


def test_compose_does_not_retain_a_full_database_backup() -> None:
    """数据库有几 GB，默认再留一份整库备份会直接翻倍占用。"""

    compose = COMPOSE.read_text(encoding="utf-8")

    assert 'AQUEST_DB_BACKUP_RETENTION_DAYS: "${AQUEST_DB_BACKUP_RETENTION_DAYS:-0}"' in compose


def test_bind_override_only_repoints_the_volume() -> None:
    """覆盖文件只改卷的落盘位置，服务定义不动，避免 volumes 列表的合并歧义。"""

    override = BIND_COMPOSE.read_text(encoding="utf-8")

    assert "\nservices:" not in override
    assert "driver_opts:" in override
    assert "device: ${DATA_DIR:?请设置 DATA_DIR 为宿主目录}" in override
    assert "chown 10001:10001" in override, "必须写明绑定挂载的属主要求"


def test_image_tag_must_be_set_explicitly() -> None:
    compose = COMPOSE.read_text(encoding="utf-8")

    assert compose.count("${AQUEST_IMAGE:?") == 2
