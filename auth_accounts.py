# auth_accounts.py
"""
Первичная авторизация всех аккаунтов из farm_config.json.
Запускать в интерактивном терминале (НЕ в фоне, НЕ через nohup).

    python auth_accounts.py

После успешной авторизации создаст sessions/<name>.session
и опционально выведет session_string для headless-режима.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from pyrogram import Client

ROOT = Path(__file__).resolve().parent
CFG_PATH = ROOT / "farm_config.json"
SESSIONS_DIR = ROOT / "sessions"
SESSIONS_DIR.mkdir(exist_ok=True)


def main() -> int:
    if not CFG_PATH.exists():
        print(f"Нет файла конфигурации: {CFG_PATH}")
        return 1

    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    accounts = cfg.get("accounts") or []
    if not accounts:
        print("В farm_config.json нет accounts")
        return 1

    print(f"Найдено аккаунтов: {len(accounts)}\n")

    for acc in accounts:
        name = acc["name"]
        print(f"=== Авторизация {name} ===")

        client = Client(
            name=name,
            api_id=int(acc["api_id"]),
            api_hash=str(acc["api_hash"]),
            phone_number=acc.get("phone"),
            workdir=str(SESSIONS_DIR),
        )

        try:
            client.start()  # спросит номер/код/2FA в терминале
            me = client.get_me()
            print(f"  ✅ Вошёл как @{me.username} (id={me.id})")

            # Полезно для headless-режима:
            session_string = client.export_session_string()
            print(f"  session_string (сохраните, если нужен headless):")
            print(f"  {session_string[:60]}...")
            print()
        except Exception as exc:
            print(f"  ❌ Ошибка авторизации {name}: {exc!r}\n")
            return 2
        finally:
            try:
                client.stop()
            except Exception:
                pass

    print("Все аккаунты авторизованы. Теперь можно запускать: python farm.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())