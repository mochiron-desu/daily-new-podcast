FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    NODE_ROLE=primary \
    STORAGE_DIR=/app/storage

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY config.yaml ./
COPY assets ./assets
COPY src ./src

RUN mkdir -p /app/storage/dossiers /app/storage/prompts /app/storage/episodes

EXPOSE 8787

CMD ["python", "-m", "src.cli", "run-once"]

