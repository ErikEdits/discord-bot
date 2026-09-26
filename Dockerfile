FROM python:3.12-slim

# Don't write .pyc, send logs unbuffered
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY bot.py server_template.py crashlog_analyzer.py ./
COPY cogs/ ./cogs/
COPY web/ ./web/

# Persistent volume target. Mounted by fly.toml.
RUN mkdir -p /app/data
VOLUME /app/data

EXPOSE 8080

CMD ["python", "bot.py"]
