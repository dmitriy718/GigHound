FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

# build context is ./worker; lay it out as an importable package
COPY . ./worker/

ENV WORKER_SESSION_DIR=/app/.sessions

CMD ["python", "-m", "worker"]
