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
    assert "python -m selector_app.strategy_discovery.worker" in compose
    assert "SELECTOR_DATA_DIR: /data/market" in compose
    assert "source: market_data" in compose
    assert "SELECTOR_DISCOVERY_WORKERS" in compose
    assert "SELECTOR_DISCOVERY_WORKERS=2" in env_example
