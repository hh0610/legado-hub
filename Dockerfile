FROM node:22-bookworm-slim AS frontend-build

WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    LEGADOHUB_BROWSER_PROVIDER=chromium \
    LEGADOHUB_BROWSER_ENABLED=1 \
    LEGADOHUB_BROWSER_PROFILE_ROOT=/app/backend/data/browser_profiles \
    PUID=1000 \
    PGID=1000

WORKDIR /app

ARG APP_UID=1000
ARG APP_GID=1000

COPY backend/requirements.txt /app/backend/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/backend/requirements.txt \
    && python -m playwright install --with-deps chromium \
    && apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid ${APP_GID} legadohub \
    && useradd --uid ${APP_UID} --gid ${APP_GID} --create-home legadohub \
    && chmod -R a+rX /ms-playwright

COPY --chown=legadohub:legadohub backend/ /app/backend/
COPY --chown=legadohub:legadohub plugins/sources/thirdparty/ /opt/legadohub/plugins/thirdparty/
COPY --chown=legadohub:legadohub --from=frontend-build /app/frontend/dist /app/frontend/dist
COPY deploy/docker/entrypoint.sh /usr/local/bin/legadohub-entrypoint

RUN chmod 755 /usr/local/bin/legadohub-entrypoint \
    && mkdir -p /app/backend/data /app/backend/config /app/backend/generated /app/backend/runtime /app/plugins/sources/official /app/plugins/sources/thirdparty \
    && chown -R legadohub:legadohub /app /home/legadohub \
    && chmod 700 /app/backend/data /app/backend/config /app/backend/generated /app/backend/runtime

WORKDIR /app/backend
EXPOSE 8765 8766
# 以 root 启动入口：按 PUID/PGID 修正挂载权限后 gosu 降权运行主进程
USER root

ENTRYPOINT ["legadohub-entrypoint"]
CMD ["python", "-m", "app.server", "--host", "0.0.0.0", "--public-port", "8765", "--admin-port", "8766"]
