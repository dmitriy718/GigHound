FROM python:3.12-slim

WORKDIR /app

# browsers land in a shared path (default is ~/.cache, root's home at build
# time) so the non-root runtime user can read them
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

# build context is ./worker; lay it out as an importable package
COPY . ./worker/

# run the browser as a non-root user (mirrors backend/Dockerfile); the
# session dir and browser cache must be writable/readable by it
RUN useradd --create-home --uid 10001 gighound \
    && mkdir -p /app/.sessions \
    && chown -R gighound:gighound /app /opt/ms-playwright
USER gighound

ENV WORKER_SESSION_DIR=/app/.sessions

CMD ["python", "-m", "worker"]
