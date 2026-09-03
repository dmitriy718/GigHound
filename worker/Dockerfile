FROM python:3.14.7-slim

WORKDIR /app

# browsers land in a shared path (default is ~/.cache, root's home at build
# time) so the non-root runtime user can read them
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright

# Install from the hashed lock (requirements.txt is the human-edited source;
# regenerate with: uv pip compile worker/requirements.txt --generate-hashes
# --python-version 3.12 -o worker/requirements.lock).
COPY requirements.lock ./requirements.lock
RUN pip install --no-cache-dir --require-hashes -r requirements.lock \
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
