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

COPY pyproject.toml README.md LICENSE THIRD_PARTY_NOTICES.md ./
COPY selector_app ./selector_app
# [catalog] 带上 baostock：东财 clist 目录接口被封后，它是唯一稳定的全市场标的
# 发现通道（docs/datasource-survey-2026-09-20.md）。没装时目录会自动回退到东财，
# 不会让启动失败，所以这个 extra 是「装上才更好」而不是「必须装」。
RUN python -m pip install --upgrade --no-cache-dir '.[catalog]' \
    && python -m pip check \
    && python -c "import selector_app; print('Indicator Lab runtime ready')"

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data/vipdoc /data/market \
    && chown -R appuser:appuser /app /data
USER appuser

EXPOSE 8123

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8123/healthz', timeout=3)"

CMD ["uvicorn", "selector_app.web.app:app", "--host", "0.0.0.0", "--port", "8123"]
