#!/usr/bin/env bash
# Безопасное обновление 0x4: код из выбранной ветки, зависимости и restart служб.
# Не удаляет/не перезаписывает .env, кошельки, SQLite базу и пользовательский config.
# Запуск от root на VPS:
#   bash /opt/0x4/deploy/updater.sh
set -Eeuo pipefail

REPO_URL="${OX4_REPO_URL:-https://github.com/Doker420/0x4.git}"
BRANCH="${OX4_BRANCH:-arena/02fe41bc-0x4}"
INSTALL_DIR="${OX4_INSTALL_DIR:-/opt/0x4}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Запустите от root: sudo bash deploy/updater.sh" >&2
  exit 1
fi

if ! command -v git >/dev/null 2>&1; then
  echo "Ошибка: git не установлен." >&2
  exit 1
fi

if [[ -e "$INSTALL_DIR" && ! -d "$INSTALL_DIR/.git" ]]; then
  echo "Ошибка: $INSTALL_DIR уже существует, но это не Git-клон. Ничего не удалено." >&2
  echo "Укажите другой OX4_INSTALL_DIR или сначала проверьте каталог вручную." >&2
  exit 1
fi

if [[ ! -d "$INSTALL_DIR/.git" ]]; then
  mkdir -p "$(dirname "$INSTALL_DIR")"
  git clone --branch "$BRANCH" --single-branch "$REPO_URL" "$INSTALL_DIR"
else
  git -C "$INSTALL_DIR" fetch origin "$BRANCH"
  current_branch="$(git -C "$INSTALL_DIR" branch --show-current)"
  if [[ "$current_branch" != "$BRANCH" ]]; then
    if ! git -C "$INSTALL_DIR" show-ref --verify --quiet "refs/heads/$BRANCH"; then
      git -C "$INSTALL_DIR" checkout -b "$BRANCH" "origin/$BRANCH"
    else
      git -C "$INSTALL_DIR" checkout "$BRANCH"
    fi
  fi
  git -C "$INSTALL_DIR" pull --ff-only origin "$BRANCH"
fi

cd "$INSTALL_DIR"
# Installer preserves .env, wallet keystore, SQLite data, and existing operator config.
bash deploy/install.sh

if command -v systemctl >/dev/null 2>&1; then
  systemctl is-active --quiet airdrop-panel.service || {
    echo "Ошибка: панель не активна. Последние логи:" >&2
    journalctl -u airdrop-panel.service -n 50 --no-pager >&2 || true
    exit 1
  }
  systemctl is-active --quiet airdrop-agent.service || {
    echo "Ошибка: фоновый агент не активен. Последние логи:" >&2
    journalctl -u airdrop-agent.service -n 50 --no-pager >&2 || true
    exit 1
  }
fi

echo ""
echo "✅ Обновление завершено: код $BRANCH, службы панели и агента перезапущены."
echo "📍 Панель: http://144.31.25.24:1111"
echo "📍 Агент работает в фоне; ежедневные ручные запуски поиска не нужны."
echo "ℹ️ Это перезапуск служб, не reboot всего VPS. Остальные сервисы не перезагружаются, если их порты уже заняты."
