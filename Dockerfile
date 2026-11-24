FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DRY_RUN=1 \
    DATABASE_PATH=/app/data/bot.db \
    IMAGES_DIR=/app/data/images \
    LOGS_DIR=/app/data/logs \
    BACKUP_DIR=/app/data/backups

WORKDIR /usr/src/app

COPY requirements.txt requirements-server.txt ./
RUN python -m pip install --no-cache-dir --disable-pip-version-check \
    -r requirements-server.txt

COPY bot ./bot
COPY start.sh ./start.sh
RUN chmod 755 ./start.sh && mkdir -p /app/data/images /app/data/logs /app/data/backups

CMD ["./start.sh"]
