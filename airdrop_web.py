#!/usr/bin/env python3
"""Запуск веб-панели управления и заработка на порту 1111 (0.0.0.0:1111).

Также автоматически следит, чтобы остальные 3 сервиса (панель 0x4 на :8080,
агент поиска airdrop.agent и Telegram-бот) были запущены.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env", override=False)

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)-16s | %(message)s",
    )
    host = os.getenv("AIRDROP_PANEL_HOST", "0.0.0.0")
    port = int(os.getenv("AIRDROP_PANEL_PORT", "1111") or "1111")
    print(f"🚀 Запуск Центра управления и заработка (порт {port}): http://{host}:{port}")
    uvicorn.run(
        "airdrop.panel:app",
        host=host,
        port=port,
        reload=False,
        log_level="info",
    )
