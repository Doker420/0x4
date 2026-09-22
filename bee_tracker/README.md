# 🐝 BEE Tracker

Watch-only мультичейн трекер портфеля и мониторинг транзакций: Telegram-бот
(aiogram 3) + REST API (FastAPI) для whitelabel-интеграций.

**62 сети из коробки:** 30 EVM · Bitcoin (legacy/segwit/taproot) · Solana ·
TON · 30 Cosmos/interchain.

---

## 🔒 Модель безопасности

Это главное архитектурное решение проекта, а не примечание мелким шрифтом.

**BEE Tracker никогда не принимает сид-фразы и приватные ключи.**

| | |
|---|---|
| Принимается | публичные адреса, расширенные **публичные** ключи (`xpub`/`ypub`/`zpub`) |
| Отклоняется с HTTP 400 | BIP-39 мнемоники, `xprv`/`yprv`/`zprv`, WIF, сырые 32-байтные hex-ключи |

Почему так, а не «зашифруем AES-GCM и всё нормально»:

- Сид-фраза, отправленная в чат, уже попала в логи Telegram, в память процесса
  и в бэкапы. Шифрование в БД это не отменяет.
- Ключ шифрования лежит в `.env` на том же сервере, что и база. Компрометация
  машины = компрометация всех средств. Это не защита, а её видимость.
- Функциональность трекера сида **не требует**: `xpub` деривит ровно те же
  адреса, но математически не способен подписать транзакцию.

Как это обеспечено в коде:

- `core/security.py` — `assert_no_secret_material()` вызывается на каждой
  пользовательской строке до записи. Текст ошибки **никогда не содержит сам
  ввод**, чтобы секрет не утёк в логи.
- `core/bip32.py` — деривация на чистом Python, реализована **только**
  `CKDpub`. Hardened-деривация выбрасывает исключение; кода подписи не
  существует.
- В схеме БД нет колонки под сид — и тест `test_no_secret_columns_in_schema`
  падает, если её кто-то добавит.
- `.env.example` не содержит `ENCRYPTION_KEY`: шифровать нечего.

> Бот не запрашивает сид-фразы. Любой, кто просит их от имени BEE Tracker, —
> мошенник.

---

## 📁 Структура

```
bee_tracker/
├── bot.py                 # aiogram 3, точка входа
├── keyboards.py
├── api/
│   ├── main.py            # FastAPI, whitelabel
│   ├── deps.py            # аутентификация по X-API-Key
│   └── routes/            # wallets · webhooks · admin
├── core/
│   ├── config.py          # настройки + лимиты тарифов
│   ├── database.py        # SQLite, учёт лимитов
│   ├── security.py        # валидация ввода, отказ от секретов
│   ├── bip32.py           # secp256k1 + BIP32 CKDpub, bech32, EIP-55
│   └── derive.py          # xpub → адреса
├── chains/                # base · evm · bitcoin · solana · cosmos · ton · registry
├── services/              # portfolio · monitor · prices · notify
├── handlers/              # start · wallets · monitor · admin
└── tests/                 # 46 тестов
```

## 📦 Зависимости

`bip-utils` **намеренно не используется**: она тянет C-расширение
`ed25519-blake2b` (требует компилятор и заголовки Python) и содержит полный
API работы с приватными ключами. Вместо неё — `core/bip32.py`: ~300 строк
чистого Python, только публичная деривация. Проверено официальными
тест-векторами BIP32 и BIP84.

---

## 🚀 Запуск

```bash
pip install -r requirements.txt
cp .env.example .env      # впишите BOT_TOKEN и ADMIN_IDS

python bot.py                                        # бот
uvicorn api.main:app --host 0.0.0.0 --port 5050      # API
```

Тесты:

```bash
pytest tests/ -q     # 46 passed
```

---

## 💎 Тарифы и лимиты

Настраиваются в `core/config.py` → `TARIFF_LIMITS` (`-1` = без лимита).

| | Free | Pro | **Team** |
|---|---|---|---|
| Кошельки | 3 | 50 | **∞** |
| Адреса мониторинга | 3 | 100 | **∞** |
| xpub | 1 | 10 | **∞** |
| Адресов на один xpub | 20 | 50 | **200** |
| Вебхуки | — | 5 | 50 |
| REST API / whitelabel | — | — | ✅ |

Тариф **Team** рассчитан на казначейство команды 5–10 человек: количество
кошельков и наблюдаемых адресов не ограничено, а один `zpub` разворачивается
сразу в 200 адресов. Вставка идёт пакетно (`add_wallets_bulk`), лимит
проверяется один раз на всю партию.

Управление:

```
/team_create <name>          # создать команду (владелец получает team)
/team_add <team_id> <uid>    # добавить участника, ему выдаётся team
/tariff <uid> <free|pro|team>
```

---

## 🌐 REST API

Аутентификация — заголовок `X-API-Key` (ключ виден в боте: ⚙️ Настройки).
Интерактивная документация: `http://host:5050/docs`.

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/health`, `/chains` | статус, список сетей |
| GET | `/portfolio` | агрегированный портфель в USD |
| GET | `/portfolio.csv` | экспорт CSV |
| GET | `/portfolio/history?days=30` | динамика по дням |
| GET/POST/DELETE | `/wallets` | адреса |
| GET/POST | `/xpubs` | публичные ключи + деривация |
| POST | `/xpubs/{id}/sync` | до-деривация при росте gap |
| GET/POST/DELETE | `/watches` | мониторинг |
| GET | `/transactions` | журнал |
| GET/POST/DELETE | `/webhooks` | вебхуки |
| GET | `/usage` | расход лимитов |
| GET/POST | `/admin/*` | статистика, тарифы, команды |

Пример:

```bash
curl -X POST localhost:5050/xpubs \
  -H "X-API-Key: bee_..." -H 'Content-Type: application/json' \
  -d '{"label":"BTC Treasury","chain":"bitcoin","xpub":"zpub6...","gap":200}'
# {"xpub_id":1,"addresses_added":200,"gap":200}
```

### Вебхуки

`POST` на ваш HTTPS-endpoint, подпись в заголовке `X-Bee-Signature:
sha256=<hmac>` — HMAC-SHA256 сырого тела на секрете вебхука. URL валидируется
против SSRF (только `https://`, приватные диапазоны запрещены).

```python
expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
assert hmac.compare_digest(expected, sig.removeprefix("sha256="))
```

События: `tx_in`, `tx_out`, `balance_change`.

---

## ⚙️ Как работает мониторинг

`services/monitor.py` опрашивает наблюдаемые адреса раз в `MONITOR_INTERVAL`
секунд (по умолчанию 120), параллельно, с ограничением в 10 одновременных
запросов. Первый проход только запоминает состояние — истории уведомлений не
будет. Дедупликация двухуровневая: в памяти на процесс и через
`UNIQUE(user_id, chain, tx_hash)` в БД, поэтому рестарт не приводит к
повторным уведомлениям. Фильтры — по сумме в USD и направлению.

---

## 📝 Замечания по эксплуатации

- **Публичные RPC** имеют лимиты и не индексируют адреса. Балансы работают
  везде; история транзакций для EVM собирается сканированием последних блоков —
  для продакшена подключите Etherscan-совместимый API или Alchemy/QuickNode.
- **CoinGecko free tier** — ~30 req/min; цены кэшируются на 60 секунд.
- В песочнице разработки внешняя сеть недоступна, поэтому онлайн-балансы
  возвращают 0. Логика деривации и лимитов покрыта офлайн-тестами.
- Развёртывание через systemd — два юнита (`bee-bot`, `bee-api`) с
  `Restart=always`; API лучше держать за nginx с TLS, а не публиковать
  порт 5050 наружу.

## 🗺 Дальше

Substrate (Polkadot/Kusama), Tron, XRP · токены ERC-20 по спискам ·
ENS/SNS-резолвинг · P&L · React-фронтенд для whitelabel.
