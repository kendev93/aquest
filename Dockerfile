# 这个 tag 就是 CI 实际测试的 Node 大版本（.github/workflows/ci.yml 的 frontend 任务从
# 本文件读它），所以升版本等于同时换掉被测工具链；`web-ui` 的 engines 只保证下限
# （>=22.22.2）。镜像与 CI 不必再各写一个数字，也就不会出现"CI 绿、镜像用另一版本构建"。
FROM node:26-alpine AS web-build

WORKDIR /webui
COPY web-ui/package.json web-ui/package-lock.json ./
RUN npm ci
COPY web-ui ./
RUN npm run build

FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# nginx 提供前端静态文件并反代 /api，supervisor 同时托管 nginx 与 uvicorn：
# 部署侧只剩一个镜像，WEB_PORT 指向容器内 nginx(8080)，API_PORT 直连 uvicorn(8123)。
RUN apt-get update \
    && apt-get install --no-install-recommends -y libgomp1 nginx supervisor \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./requirements.txt
RUN python -m pip install --no-cache-dir --require-hashes -r requirements.txt

COPY README.md LICENSE THIRD_PARTY_NOTICES.md ./
COPY aquest ./aquest
# 运行时刻意不把 aquest 装成 Python 包：源码已经在 /app 里、WORKDIR 就是 /app，
# `import aquest` 直接可用；镜像里也没有任何地方读 aquest 自身的包元数据
# （strategy_discovery/service.py 的 _package_version 只查 optuna 和 lightgbm）。
#
# 所以这里不走 `pip install '.[catalog]'`：那会触发 PEP 517 构建隔离，每次构建都要额外
# 联网拉 hatchling，代理一关就 SSL EOF 失败，而装出来的东西我们根本用不上。
# catalog extra 里唯一的包 baostock（全市场标的发现通道，见
# docs/datasource-survey-2026-09-20.md）已并入 requirements.txt，跟着上面那层一起装。
#
# 副作用：源码变化不再让 pip 重装，构建只剩「拉 baostock 等 wheel」这一处联网点。
RUN python -m pip check \
    && python -c "import aquest, baostock; print('AQuest runtime ready')"

# 前端静态资源由 node 阶段构建，nginx 的站点根目录就是它。
COPY --from=web-build /webui/dist/ /usr/share/nginx/html/
COPY docker/nginx.conf /etc/nginx/nginx.conf
COPY docker/supervisord.conf /etc/supervisor/conf.d/supervisord.conf

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data/vipdoc /data/market \
    && chown -R appuser:appuser /app /data /usr/share/nginx/html
USER appuser

EXPOSE 8123 8080

# 宽限给到 60 分钟：启动会在绑定端口之前做一次全库压缩（COPY FROM DATABASE 整库重写），
# 而这是有意为之——交换数据文件必须在还没有读取者的时候做。本轮实测（2026-10-18，昨天那次
# 「一键修复」删并重写 113 万根之后触发）4.69GB 压到 3.06GB 用了约 6 分钟；但共享卷的写入
# 速度受缓存与宿主负载影响，慢的场景能到几十分钟，所以按上限留足余量。旧值
# start-period=20s + retries=3 约 110 秒就把容器判成 unhealthy，于是合法压缩被报成「起不来」，
# `depends_on: service_healthy` 的 worker 卡在 Created。
# 代价：真的启动失败也要等满 60 分钟才被标红，但 `docker compose ps` 会一直显示 starting，
# 日志里 `Waiting for application startup.` 迟迟不消失就是异常信号，不必等健康检查下结论。
# 过了宽限期之后恢复成 3 次连败即失败（约 90 秒），服务中途真的坏掉仍能被及时发现。
HEALTHCHECK --interval=30s --timeout=5s --start-period=3600s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"

CMD ["supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
