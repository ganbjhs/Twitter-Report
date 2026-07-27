# Report Automation — the whole app, browser included.
#
# Chromium runs here, on your own server. That is the original design: no remote
# browser service, no per-minute cap, no extra network hop.
#
# Base: the official Playwright Python image — Chromium and every OS library it
# needs are already installed, and it is published for amd64 AND arm64. That
# avoids the fragile `playwright install --with-deps` step at build time.
# The tag MUST match the playwright version in requirements.txt.
FROM mcr.microsoft.com/playwright/python:v1.61.0-noble

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000 \
    DATA_DIR=/app/data \
    SESSIONS_DIR=/app/sessions \
    HOME=/app

WORKDIR /app

# Dependencies first so code edits don't invalidate the layer.
COPY requirements.txt requirements-web.txt ./
RUN pip install --no-cache-dir --break-system-packages \
        -r requirements.txt -r requirements-web.txt

# The frozen X pipeline, the influencer pipeline, and the web layer.
COPY run.py save_login.py install.py ./
COPY src/ ./src/
COPY influencer/ ./influencer/
COPY webapp/ ./webapp/

# Runtime state. docker-compose bind-mounts host folders over these, so the X
# login and every generated report survive restarts and are readable on the
# server's disk.
#
# Run as UID 1000 — it matches the default non-root user on an Ubuntu VPS, so a
# bind-mounted ./sessions is writable without a chown dance.
RUN mkdir -p /app/data /app/sessions /app/reports \
 && if ! id -u 1000 >/dev/null 2>&1; then useradd -m -u 1000 appuser; fi \
 && chown -R 1000:0 /app \
 && chmod -R g+rwX /app

USER 1000

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/health').read()"

# One worker process: job state lives in this process's thread pool, so scaling
# means raising MAX_CONCURRENT_JOBS / WORKERS, not adding uvicorn workers.
CMD ["sh", "-c", "exec uvicorn webapp.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
