# web_run.py
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
import uvicorn
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
BRIDGE_DIR = ROOT / "vendor" / "Deepseek-API"
if BRIDGE_DIR.exists():
    sys.path.insert(0, str(BRIDGE_DIR))

load_dotenv(ROOT / ".env")

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)-14s | %(message)s",
    )
    host = os.getenv("WEB_HOST", "0.0.0.0")
    port = int(os.getenv("WEB_PORT", "8080"))

    print(f"🚀 Запуск веб-панели управления фермой: http://{host}:{port}")
    uvicorn.run(
        "web.app:app",
        host=host,
        port=port,
        reload=False,
        log_level="info",
    )
