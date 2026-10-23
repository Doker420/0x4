# Аирдроп-агент + Веб-панель :1111 + Telegram-бот (0x4).
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash agent

WORKDIR /app

COPY requirements-web.txt requirements-airdrop.txt ./
RUN python -m pip install --upgrade pip && python -m pip install -r requirements-airdrop.txt

COPY airdrop/ ./airdrop/
COPY config/ ./config/
COPY bot_airdrop.py airdrop_web.py ./

RUN chown -R agent:agent /app && mkdir -p /app/data && chown agent:agent /app/data

USER agent
EXPOSE 1111
VOLUME ["/app/data", "/app/config"]

CMD ["python", "airdrop_web.py"]
