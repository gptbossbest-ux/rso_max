FROM python:3.12-slim

ARG APP_UID=1000
ARG APP_GID=1000
ARG VCS_REF=unknown

LABEL org.opencontainers.image.revision="$VCS_REF"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY certs/russian_trusted_root_ca.crt \
    /usr/local/share/ca-certificates/russian_trusted_root_ca.crt
RUN update-ca-certificates

RUN if ! getent group "$APP_GID" >/dev/null; then groupadd --gid "$APP_GID" app; fi \
    && useradd --uid "$APP_UID" --gid "$APP_GID" --create-home --shell /usr/sbin/nologin app

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir --requirement requirements.txt

COPY --chown=${APP_UID}:${APP_GID} . .
RUN mkdir -p /app/data /app/logs /app/backups /app/KV \
    && chown -R "$APP_UID:$APP_GID" /app/data /app/logs /app/backups

USER app

EXPOSE 5000 8000
