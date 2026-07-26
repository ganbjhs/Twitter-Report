# Report Automation — the THIN image: app only, no browser.
#
# This is the image for a free host (Render's free tier is ~512 MB RAM, which
# cannot run Chromium). The browser lives on a remote service and is reached
# over CDP, so this image only needs the Playwright *client* library — roughly
# 200 MB instead of 4 GB, which also makes cold starts far quicker.
#
# It REQUIRES these at runtime, or captures will try to launch a browser that
# is not installed:
#     BROWSER_BACKEND=browserless
#     BROWSERLESS_WS=wss://<region>.browserless.io?token=<TOKEN>
#
# Want the browser bundled instead (a VM, or running locally)? Build the sibling
# file, which is the same app on the Playwright base image:
#     docker build -f Dockerfile.chromium -t report-automation .

FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000 \
    DATA_DIR=/app/data \
    SESSIONS_DIR=/app/sessions \
    HOME=/app \
    BROWSER_BACKEND=browserless \
    EXECUTION_MODE=inline

WORKDIR /app

# Only what the app itself needs. No Chromium, and none of its OS libraries —
# `pip install playwright` gives the client library; the browser is remote.
COPY requirements.txt requirements-web.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-web.txt

COPY run.py save_login.py install.py ./
COPY src/ ./src/
COPY influencer/ ./influencer/
COPY webapp/ ./webapp/

# Runtime state. The disk is ephemeral on a free host, which is fine: the X
# session is mirrored to an external store (webapp/x_state_store.py) and
# finished reports are downloaded straight away.
RUN mkdir -p /app/data /app/sessions \
 && useradd -m -u 1000 appuser \
 && chown -R 1000:0 /app \
 && chmod -R g+rwX /app

USER 1000

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/health').read()"

CMD ["sh", "-c", "exec uvicorn webapp.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
