FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.lock ./requirements.lock
RUN python -m pip install --no-cache-dir -r requirements.lock

COPY pyproject.toml README.md LICENSE THIRD_PARTY_NOTICES.md ./
COPY selector_app ./selector_app
RUN python -m pip install --no-cache-dir --no-deps .

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data/vipdoc /data/market \
    && chown -R appuser:appuser /app /data
USER appuser

EXPOSE 8123

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8123/healthz', timeout=3)"

CMD ["uvicorn", "selector_app.web.app:app", "--host", "0.0.0.0", "--port", "8123"]
