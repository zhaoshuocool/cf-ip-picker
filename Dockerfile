FROM python:3.12-alpine

LABEL org.opencontainers.image.title="cf-ip-picker" \
      org.opencontainers.image.description="Pick the fastest reachable Cloudflare edge IP for your Tunnel + VLESS node" \
      org.opencontainers.image.source="https://github.com/zhaoshuocool/cf-ip-picker" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    CF_PATH=/ \
    CF_INTERVAL=3600 \
    CF_RANDOM=60 \
    CF_TIMEOUT=3

WORKDIR /app
COPY app/main.py /app/main.py

RUN adduser -D -H -u 10001 picker \
    && mkdir -p /data && chown 10001:10001 /data
USER 10001

EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/healthz'%os.environ.get('PORT','8080'),timeout=4)" || exit 1

CMD ["python", "-u", "/app/main.py"]
