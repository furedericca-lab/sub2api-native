FROM node:22-bookworm-slim AS frontend-builder
WORKDIR /build/front
COPY front/package.json front/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
COPY front/ ./
RUN npm run build

FROM ubuntu:24.04 AS python-builder
ARG DEBIAN_FRONTEND=noninteractive
ENV PATH=/opt/sub2api-venv/bin:$PATH \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    XDG_CACHE_HOME=/opt/camoufox-cache

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates python3 python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt ./
COPY vendor/outlookEmail/requirements.txt ./outlookemail-requirements.txt
RUN python3 -m venv /opt/sub2api-venv \
    && python3 -m venv /opt/outlookemail-venv \
    && /opt/sub2api-venv/bin/pip install --upgrade pip \
    && /opt/outlookemail-venv/bin/pip install --upgrade pip \
    && /opt/sub2api-venv/bin/pip install -r requirements.txt \
    && /opt/outlookemail-venv/bin/pip install -r outlookemail-requirements.txt gunicorn \
    && /opt/sub2api-venv/bin/pip check \
    && /opt/outlookemail-venv/bin/pip check

COPY docker/fetch_camoufox.py ./

# 浏览器引擎直接内置到镜像，容器首次启动时不再临时下载。Camoufox CLI
# 对下载异常可能返回 0，因此这里显式校验可执行文件；失败时重试后再终止构建。
RUN set -eux; \
    for attempt in 1 2 3; do \
        timeout --kill-after=5s 300s python /build/fetch_camoufox.py || true; \
        if python -c 'from camoufox.pkgman import camoufox_path, launch_path; path = camoufox_path(download_if_missing=False); print(launch_path(path))'; then \
            python -m camoufox version; \
            exit 0; \
        fi; \
        rm -rf /opt/camoufox-cache/camoufox; \
        sleep "$attempt"; \
    done; \
    echo "Camoufox browser installation failed after retries" >&2; \
    exit 1

FROM ubuntu:24.04 AS runtime
ARG DEBIAN_FRONTEND=noninteractive
ARG APP_UID=10001
ARG APP_GID=10001

ENV PATH=/opt/sub2api-venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/home/app \
    XDG_CACHE_HOME=/opt/camoufox-cache \
    DISPLAY=:99 \
    SUB2API_WEB_HOST=0.0.0.0 \
    SUB2API_WEB_PORT=8787 \
    SUB2API_CONFIG_FILE=/app/data/config.json \
    SUB2API_FORCE_HEADED=1

# Camoufox/Firefox 有头模式依赖 + Xvfb 虚拟显示器。
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates dumb-init gosu procps python3 xvfb xauth \
        libasound2t64 libatk1.0-0t64 libavcodec60 \
        libatk-bridge2.0-0t64 libatspi2.0-0t64 libcups2t64 \
        libcairo-gobject2 libcairo2 libdbus-1-3 \
        libdrm2 libfontconfig1 libfreetype6 libgbm1 libgdk-pixbuf-2.0-0 \
        libglib2.0-0t64 libgtk-3-0t64 libpango-1.0-0 \
        libpangocairo-1.0-0 libx11-6 libx11-xcb1 \
        libxcb-shm0 libxcb1 libxcomposite1 libxcursor1 \
        libxdamage1 libxext6 libxfixes3 libxi6 libxrandr2 \
        libxkbcommon0 libxrender1 libxshmfence1 libxss1 libxtst6 \
        libnspr4 libnss3 fonts-freefont-ttf fonts-liberation \
        fonts-noto-color-emoji fonts-unifont fonts-wqy-zenhei \
    && groupadd --gid "$APP_GID" app \
    && useradd --uid "$APP_UID" --gid "$APP_GID" --create-home --shell /bin/bash app \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --chown=app:app --from=python-builder /opt/sub2api-venv /opt/sub2api-venv
COPY --chown=app:app --from=python-builder /opt/outlookemail-venv /opt/outlookemail-venv
COPY --chown=app:app --from=python-builder /opt/camoufox-cache /opt/camoufox-cache
COPY --chown=app:app backend/ ./backend/
COPY --chown=app:app vendor/outlookEmail/ ./vendor/outlookEmail/
COPY --chown=app:app config.example.json requirements.txt ./
COPY --chown=app:app --from=frontend-builder /build/front/dist ./front/dist/
COPY --chown=app:app --chmod=755 docker/entrypoint.sh ./docker/entrypoint.sh
COPY --chown=app:app docker/config_bootstrap.py ./docker/config_bootstrap.py
COPY --chown=app:app docker/camoufox_smoke.py ./docker/camoufox_smoke.py
COPY --chown=app:app scripts/check-outlookemail-contract.py ./scripts/check-outlookemail-contract.py
COPY --chown=app:app scripts/sync-outlookemail-runtime-password.py ./scripts/sync-outlookemail-runtime-password.py

RUN install -d -o app -g app /app/data /app/data/outlookemail /app/logs /tmp/sub2api-native-camoufox

VOLUME ["/app/data", "/app/logs"]
EXPOSE 8787 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; [urllib.request.urlopen(url, timeout=3).read() for url in ('http://127.0.0.1:8787/api/health', 'http://127.0.0.1:5000/')]"

ENTRYPOINT ["/usr/bin/dumb-init", "--", "/app/docker/entrypoint.sh"]
CMD ["python", "-m", "backend.web.cli", "--host", "0.0.0.0", "--port", "8787"]
