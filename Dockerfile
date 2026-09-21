FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install --no-install-recommends -y libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.lock ./requirements.lock
RUN python -m pip install --upgrade --no-cache-dir -r requirements.lock

COPY README.md LICENSE THIRD_PARTY_NOTICES.md ./
COPY selector_app ./selector_app
# 运行时刻意不把 selector_app 装成 Python 包：源码已经在 /app 里、WORKDIR 就是 /app，
# `import selector_app` 直接可用；镜像里也没有任何地方读 indicator-lab 自身的包元数据
# （strategy_discovery/service.py 的 _package_version 只查 optuna 和 lightgbm）。
#
# 所以这里不走 `pip install '.[catalog]'`：那会触发 PEP 517 构建隔离，每次构建都要额外
# 联网拉 hatchling，代理一关就 SSL EOF 失败，而装出来的东西我们根本用不上。
# catalog extra 里唯一的包 baostock（全市场标的发现通道，见
# docs/datasource-survey-2026-09-20.md）已并入 requirements.lock，跟着上面那层一起装。
#
# 副作用：源码变化不再让 pip 重装，构建只剩「拉 baostock 等 wheel」这一处联网点。
RUN python -m pip check \
    && python -c "import selector_app, baostock; print('Indicator Lab runtime ready')"

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data/vipdoc /data/market \
    && chown -R appuser:appuser /app /data
USER appuser

EXPOSE 8123

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8123/healthz', timeout=3)"

CMD ["uvicorn", "selector_app.web.app:app", "--host", "0.0.0.0", "--port", "8123"]
