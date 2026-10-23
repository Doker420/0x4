#!/usr/bin/env bash
# Однокомандная установка/обновление всех 4 сервисов проекта 0x4 на VPS (144.31.25.24):
#   1) airdrop-panel  — Веб-панель управления, кошельков (Exodus) и прогноза на порту :1111
#   2) 0x4-web        — Веб-панель Telegram-аккаунтов и фермы на порту :8080
#   3) airdrop-agent  — Агент 24/7 поиска аирдропов и проверки кошельков
#   4) airdrop-bot    — Telegram-бот управления
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "==> Каталог проекта: $ROOT"

if ! command -v python3 >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1 && [ "$(id -u)" -eq 0 ]; then
    apt-get update -y && apt-get install -y python3 python3-venv python3-pip ca-certificates curl
  else
    echo "Нужен python3 (3.10+)." >&2
    exit 1
  fi
fi

if ! python3 -m venv --help >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1 && [ "$(id -u)" -eq 0 ]; then
    apt-get update -y && apt-get install -y python3-venv ca-certificates
  fi
fi

if [ ! -d .venv ]; then
  echo "==> Создаю виртуальное окружение .venv"
  python3 -m venv .venv
fi

echo "==> Устанавливаю зависимости"
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt -r requirements-airdrop.txt

mkdir -p data/airdrop/keystore config sessions
chmod 700 data/airdrop data/airdrop/keystore

if [ ! -f config/airdrops.json ]; then
  cp config/airdrops.example.json config/airdrops.json
fi
if [ ! -f config/contracts.json ]; then
  cp config/contracts.example.json config/contracts.json
fi

if [ ! -f .env ]; then
  cp .env.example .env
fi
chmod 600 .env

if ! grep -q "^AIRDROP_KEY_PASSPHRASE=.." .env 2>/dev/null; then
  PASSPHRASE="$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(24))')"
  grep -v "^AIRDROP_KEY_PASSPHRASE=" .env > .env.tmp || true
  mv .env.tmp .env
  echo "AIRDROP_KEY_PASSPHRASE=${PASSPHRASE}" >> .env
  chmod 600 .env
  echo "==> Сгенерирована AIRDROP_KEY_PASSPHRASE в .env"
fi

# Создаём стартовый кошелёк main (если ещё нет), чтобы сразу был готов для Exodus
.venv/bin/python - <<'PY'
from airdrop.panel import ensure_panel_passphrase
from airdrop import wallet
ensure_panel_passphrase()
if not wallet.list_wallets():
    info = wallet.create_wallet("main")
    print("==> Создан кошелёк main:", info["address"])
else:
    print("==> Кошельки в хранилище:", [w["name"] for w in wallet.list_wallets()])
PY

echo "==> Первичное наполнение базы находок"
.venv/bin/python -m airdrop.agent --once --offline || true

# Открываем порты 1111 и 8080 в ufw, если файрвол включён
if command -v ufw >/dev/null 2>&1 && [ "$(id -u)" -eq 0 ]; then
  if ufw status | grep -q "Status: active"; then
    ufw allow 1111/tcp || true
    ufw allow 8080/tcp || true
  fi
fi

if [ "${INSTALL_SYSTEMD:-1}" = "1" ] && command -v systemctl >/dev/null 2>&1 && [ "$(id -u)" -eq 0 ]; then
  RUN_USER="${SUDO_USER:-root}"
  RUN_GROUP="$(id -gn "$RUN_USER")"
  for unit in airdrop-panel.service 0x4-web.service airdrop-agent.service airdrop-bot.service; do
    sed \
      -e "s|@USER@|${RUN_USER}|g" \
      -e "s|@GROUP@|${RUN_GROUP}|g" \
      -e "s|@INSTALL_DIR@|${ROOT}|g" \
      "deploy/${unit}" > "/etc/systemd/system/${unit}"
  done
  systemctl daemon-reload
  systemctl enable airdrop-panel.service airdrop-agent.service 0x4-web.service airdrop-bot.service || true
  systemctl restart airdrop-panel.service
  systemctl restart airdrop-agent.service

  # Запускаем 0x4-web (:8080), только если порт 8080 ещё не занят другим вашим сервисом
  if ! ss -tln 2>/dev/null | grep -q ':8080 '; then
    systemctl restart 0x4-web.service || true
  else
    echo "==> Порт 8080 уже слушается существующим сервисом — не перезапускаю его, чтобы не прервать работу."
  fi

  if grep -qE '^AIRDROP_BOT_TOKEN=.+' .env && grep -qE '^(AIRDROP_ADMIN_IDS|ADMIN_IDS)=[0-9]+' .env; then
    systemctl restart airdrop-bot.service || true
  fi

  echo ""
  echo "=================================================================="
  echo "✅ ГОТОВО! Все сервисы установлены и запущены."
  echo "   🌐 Новая панель управления (:1111): http://144.31.25.24:1111"
  echo "   🎛 Панель Telegram-фермы (:8080):   http://144.31.25.24:8080"
  echo "=================================================================="
fi
