"""Forecasting, earning streams (beyond just airdrops), and beginner checklists."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from . import db, wallet
from .settings import settings

# 5 легальных направлений заработка (от быстрых денег сегодня до крупных раздач).
EARNING_STREAMS: List[Dict[str, Any]] = [
    {
        "id": "quick_quests",
        "badge": "⚡ Быстрые деньги (День 1–3)",
        "title": "Learn & Earn + Квесты с мгновенным пулом ($1–$20 без вложений)",
        "capital": "$0",
        "time_to_first_dollar": "Сегодня / 1–48 часов",
        "monthly_range": "$10 – $60",
        "difficulty": "Очень легко (ответы на тесты и простые задания)",
        "why_it_works": (
            "Крупные биржи и квест-платформы платят из маркетингового бюджета по $1–$5 за прохождение "
            "короткого урока (Learn & Earn) или простого ончейн-квеста. Это единственный честный способ "
            "увидеть первые $1–$10 почти сразу, не дожидаясь месяцами снапшота."
        ),
        "platforms": [
            {
                "name": "Binance Academy — Learn & Earn",
                "url": "https://academy.binance.com/en/learn-and-earn",
                "payout": "$1–$10 в токенах сразу после квиза",
                "note": "Нужен верифицированный аккаунт Binance; новые квизы выходят раз в 1–2 недели.",
            },
            {
                "name": "Coinbase / Coinbase Wallet Quests",
                "url": "https://www.coinbase.com/learning-rewards",
                "payout": "$3–$15 за короткие задания",
                "note": "Работает в поддерживаемых странах и в мобильном кошельке Coinbase Wallet.",
            },
            {
                "name": "Layer3.xyz — Quests & Bounties",
                "url": "https://app.layer3.xyz/",
                "payout": "Мгновенные кубы/XP + розыгрыши USDC/токенов",
                "note": "Подключаешь свой кошелёк (тот самый, что мы создали) и проходишь бесплатные квесты.",
            },
            {
                "name": "Galxe Quest (фильтр: Token / USDT Reward)",
                "url": "https://app.galxe.com/quest",
                "payout": "$1–$25 в пулах с авто-раздачей (Raffle / FCFS)",
                "note": "Выбирай кампании с наградой в USDT/USDC и небольшим числом участников.",
            },
            {
                "name": "Intract.io — Quests",
                "url": "https://www.intract.io/",
                "payout": "XP + пулы в USDC для новых проектов",
                "note": "Хорошо подходит для старта с нуля без газа.",
            },
        ],
    },
    {
        "id": "telegram_0x4_service",
        "badge": "💼 Самый стабильный кэш (Неделя 1)",
        "title": "Услуги на базе твоей панели 0x4 (Telegram + Мониторинг)",
        "capital": "$0 (сервер 144.31.25.24 уже есть!)",
        "time_to_first_dollar": "1–5 дней",
        "monthly_range": "$50 – $350+",
        "difficulty": "Легко (всё уже написано, ты только нажимаешь кнопки)",
        "why_it_works": (
            "У тебя на сервере уже крутятся ДВЕ готовые системы: панель управления Telegram-чатами (:8080) "
            "и радар аирдропов с ботом (:1111). Тысячи админов мелких Telegram-каналов и крипто-чатов не умеют "
            "поднимать сервер, но готовы платить $15–$50 за настройку или ведение активности."
        ),
        "platforms": [
            {
                "name": "Kwork.ru (рунет фриланс по фикс-цене от 500–3000 ₽)",
                "url": "https://kwork.ru/",
                "payout": "$10–$40 за 1 заказ",
                "note": "Кворк: «Установлю личного Telegram-бота и панель мониторинга крипто-аирдропов на ваш VPS».",
            },
            {
                "name": "Модерация и оживление легальных Telegram-комьюнити (:8080)",
                "url": "http://144.31.25.24:8080",
                "payout": "$30–$150/мес с одного клиента",
                "note": "Помогаешь владельцам открытых групп поддерживать живое общение по согласованию с участниками.",
            },
        ],
    },
    {
        "id": "testnets_zero",
        "badge": "🧪 Без вложений ($0 старт)",
        "title": "Оплачиваемые тестнеты и Ранние поинт-сезоны",
        "capital": "$0 (тестовые токены бесплатны из крана / faucet)",
        "time_to_first_dollar": "3–10 недель (после выхода токена)",
        "monthly_range": "$0 в первый месяц → $50–$400 при выходе проекта",
        "difficulty": "Легко (по 10–15 минут в день)",
        "why_it_works": (
            "Новые блокчейны перед запуском раздают бесплатные тестовые монеты и просят протестировать "
            "свопы/бриджи. За это начисляют поинты или роли в Discord, которые при запуске сети меняются на реальные токены."
        ),
        "platforms": [
            {
                "name": "Наш авто-радар в разделе «Находки» (/drops)",
                "url": "/drops?cat=testnet",
                "payout": "$20–$300 с 1 удачного проекта",
                "note": "Агент сам ищет новые раунды и тестнеты каждые 90 минут и присылает в бота.",
            },
        ],
    },
    {
        "id": "retro_airdrops",
        "badge": "🪂 Классические ретро-дропы (L2)",
        "title": "Активность в дешёвых сетях (Base, Arbitrum, Scroll, ZkSync)",
        "capital": "$5–$25 (лежат на твоём же кошельке в ETH/USDC, на газ уходит по $0.01–$0.05)",
        "time_to_first_dollar": "1–4 месяца",
        "monthly_range": "$30 – $500 (залпами при раздачах)",
        "difficulty": "Средне (раз в неделю делать 2–3 простых обмена)",
        "why_it_works": (
            "Протоколы из списка DefiLlama без своего токена (наш агент собирает их автоматически!) "
            "раздают токены реальным пользователям, которые пользовались ими хотя бы 1–2 раза в неделю."
        ),
        "platforms": [
            {
                "name": "DefiLlama Airdrops (автоматически в нашей базе)",
                "url": "https://defillama.com/airdrops",
                "payout": "$50–$1000+ за сезон на живой кошелёк",
                "note": "Делай действия со своего основного кошелька (подключённого в Exodus/Rabby), не плоди сотни пустых.",
            },
        ],
    },
    {
        "id": "depin_vps",
        "badge": "🖥 Бонус от сервера 144.31.25.24",
        "title": "Полезная работа сервера 24/7 (Авто-мониторинг + алерты)",
        "capital": "$0 сверх стоимости твоего VPS",
        "time_to_first_dollar": "Постоянная экономия времени",
        "monthly_range": "Увеличивает шанс не пропустить клейм на +200%",
        "difficulty": "Автоматически (уже настроено)",
        "why_it_works": (
            "Большинство новичков теряют деньги потому, что узнают о раздаче после закрытия клейма (дедлайна). "
            "Твой агент на 144.31.25.24 не спит: он проверяет источники и твои кошельки каждые несколько часов."
        ),
        "platforms": [],
    },
]

# Стартовые карточки в базу находок, чтобы новичок сразу видел и быстрые $1–$15, и крупные дропы.
STARTER_OPPORTUNITIES: List[Dict[str, Any]] = [
    {
        "name": "⚡ Layer3 — Бесплатные квесты и ежедневные кубы (старт $0)",
        "url": "https://app.layer3.xyz/",
        "source": "starter",
        "chain": "base",
        "category": "quick-earn",
        "score": 9.4,
        "summary": "Подключи наш кошелёк, проходи квесты без вложений (квизы и бесплатные шаги). Прогревает кошелёк в сети Base/Arbitrum и даёт участие в пулах наград.",
        "deadline": None,
    },
    {
        "name": "⚡ Binance Academy — Learn & Earn (быстрые $1–$10 за ответы на тесты)",
        "url": "https://academy.binance.com/en/learn-and-earn",
        "source": "starter",
        "chain": "bsc",
        "category": "quick-earn",
        "score": 9.2,
        "summary": "Самый быстрый способ получить первые легальные доллары: читаешь короткий урок на 3 минуты, отвечаешь на тест — получаешь токены прямо на спотовый счёт.",
        "deadline": None,
    },
    {
        "name": "⚡ Galxe Quest — Кампании с призовым пулом в USDT/USDC",
        "url": "https://app.galxe.com/quest",
        "source": "starter",
        "chain": "polygon",
        "category": "quick-earn",
        "score": 8.9,
        "summary": "Квесты от новых крипто-проектов с розыгрышем и раздачей реальных USDT/USDC за подписку и проверку кошелька.",
        "deadline": None,
    },
    {
        "name": "⚡ Intract Quests — Задания для новичков с пулами в USDC",
        "url": "https://www.intract.io/",
        "source": "starter",
        "chain": "base",
        "category": "quick-earn",
        "score": 8.6,
        "summary": "Ежедневные чек-ины и простые квесты для набора XP и участия в быстрых пулах наград.",
        "deadline": None,
    },
]

# Пошаговый чек-лист новичка («Что мне делать прямо сейчас»).
BEGINNER_CHECKLIST: List[Dict[str, str]] = [
    {
        "id": "step_wallet",
        "title": "Шаг 1. Создать свой первый кошелёк во вкладке «👛 Кошельки»",
        "detail": "Нажми «Создать кошелёк» (например, назови его main). Агент создаст адрес 0x… и зашифрует ключ на сервере.",
    },
    {
        "id": "step_exodus",
        "title": "Шаг 2. Подключить этот кошелёк себе в телефон/ПК (Exodus + Rabby/MetaMask)",
        "detail": "Во вкладке «👛 Кошельки» нажми «Подключить в Exodus / Показать 12 слов», запиши 12 слов на бумагу и вставь их в приложение Exodus.",
    },
    {
        "id": "step_bot",
        "title": "Шаг 3. Подключить своего Telegram-бота для уведомлений",
        "detail": "Создай бота у @BotFather за 1 минуту, вставь токен и свой Telegram ID на главной странице панели — теперь сервер сам пишет тебе в Telegram.",
    },
    {
        "id": "step_quick_dollar",
        "title": "Шаг 4. Забрать первые быстрые $1–$10 на Learn & Earn и бесплатных квестах",
        "detail": "Открой вкладку «🪂 Находки» → фильтр «⚡ Быстрые ($0)» и пройди первые 2–3 бесплатных квиза/квеста (Layer3 / Galxe / Binance Learn).",
    },
    {
        "id": "step_scan_watch",
        "title": "Шаг 5. Выбрать 3–5 проектов «В работу» и проверять панель 10 минут в день",
        "detail": "Нажми «🔄 Запустить живой поиск» на дашборде, отметь понравившиеся находки кнопкой «✅ В работу» — агент будет следить за ними за тебя.",
    },
]


async def get_checklist_state() -> List[Dict[str, Any]]:
    raw = await db.get_kv("beginner_checklist", "{}")
    try:
        done_map = json.loads(raw) if raw else {}
    except ValueError:
        done_map = {}
    out: List[Dict[str, Any]] = []
    for item in BEGINNER_CHECKLIST:
        out.append(
            {
                **item,
                "done": bool(done_map.get(item["id"], False)),
            }
        )
    return out


async def toggle_checklist_item(item_id: str, done: Optional[bool] = None) -> List[Dict[str, Any]]:
    raw = await db.get_kv("beginner_checklist", "{}")
    try:
        done_map = json.loads(raw) if raw else {}
    except ValueError:
        done_map = {}
    current = bool(done_map.get(item_id, False))
    done_map[item_id] = (not current) if done is None else bool(done)
    await db.set_kv("beginner_checklist", json.dumps(done_map, ensure_ascii=False))
    return await get_checklist_state()


async def seed_starter_opportunities() -> int:
    """Ensure beginner-friendly quick-earning opportunities exist in the DB."""
    from .models import Airdrop

    added = 0
    for row in STARTER_OPPORTUNITIES:
        item = Airdrop(
            name=row["name"],
            url=row["url"],
            source=row["source"],
            chain=row["chain"],
            category=row["category"],
            score=float(row["score"]),
            summary=row["summary"],
            deadline=row["deadline"],
        )
        if await db.upsert_project(item, score=float(row["score"])):
            added += 1
    return added


async def build_project_forecast(
    hours_per_day: float = 1.0,
    budget_usd: float = 0.0,
    include_services: bool = True,
) -> Dict[str, Any]:
    """Calculate a transparent, realistic forecast for the operator's setup."""
    hours = max(0.25, min(float(hours_per_day or 1.0), 8.0))
    budget = max(0.0, min(float(budget_usd or 0.0), 10_000.0))

    stats = await db.stats()
    wallets = wallet.list_wallets()
    checklist = await get_checklist_state()
    done_steps = sum(1 for item in checklist if item["done"])

    # Readiness score (0..100) of our technical setup on 144.31.25.24
    readiness = 25  # panel + agent code installed
    if wallets:
        readiness += 25
    if stats.get("projects", 0) >= 4:
        readiness += 20
    if settings.bot_token and settings.admin_ids:
        readiness += 15
    readiness += min(15, done_steps * 3)
    readiness = min(100, readiness)

    # Time factor (diminishing returns so we never promise unrealistic numbers)
    time_factor = min(2.4, (hours / 1.0) ** 0.75)
    capital_factor = 1.0 + min(1.8, (budget / 50.0) ** 0.5) if budget > 0 else 1.0

    # There is no defensible income estimate for airdrops, quests, or freelance work.
    # A zero floor is honest: rewards may be unavailable, restricted, or never paid.
    day_min = day_likely = 0
    day_max = 0
    week_min = week_likely = week_max = 0
    month_min = month_likely = month_max = 0
    half_year_min = half_year_likely = half_year_max = 0

    verdict = (
        "Панель показывает инструменты поиска и проверки, но не гарантирует найденные раздачи, "
        "выплаты, доступность Learn & Earn или клиентов. До фактического получения средств "
        "реалистичная гарантированная сумма — $0. Все предложения нужно проверять на официальных "
        "сайтах; комиссии, ограничения по стране и риск потери средств учитывайте отдельно."
    )

    return {
        "inputs": {
            "hours_per_day": hours,
            "budget_usd": budget,
            "include_services": include_services,
        },
        "readiness_pct": readiness,
        "wallets_count": len(wallets),
        "projects_count": stats.get("projects", 0),
        "watching_count": len(await db.list_projects(status="watching", limit=100)),
        "done_steps": done_steps,
        "total_steps": len(checklist),
        "horizons": [
            {
                "period": "Первые 24–48 часов (Цель «Первые $1–$5»)",
                "min": day_min,
                "likely": day_likely,
                "max": day_max,
                "source": "Learn & Earn квизы (Binance / Coinbase / Revolut) + быстрые квесты Layer3/Galxe",
                "probability": "Высокая (75–85%), если реально пройти 2–3 квиза/квеста руками",
            },
            {
                "period": "1 неделя",
                "min": int(week_min),
                "likely": int(week_likely),
                "max": int(week_max),
                "source": "Квесты с пулами USDT/USDC + первый мелкий заказ/модерация через стек 0x4",
                "probability": "Средне-высокая (65%)",
            },
            {
                "period": "1 месяц",
                "min": int(month_min),
                "likely": int(month_likely),
                "max": int(month_max),
                "source": "Регулярные квест-пулы + поинт-сезоны + 2–3 заказа по Telegram-панели",
                "probability": "Стабильная при ежедневных 15–30 мин",
            },
            {
                "period": "3–6 месяцев (Горизонт выхода ретро-дропов)",
                "min": int(half_year_min),
                "likely": int(half_year_likely),
                "max": int(half_year_max),
                "source": "Выплаты (TGE/Claim) по 2–4 проектам из списка «В работе» + накопленный доход",
                "probability": "Залповая (1–2 крупных клейма дают основную сумму)",
            },
        ],
        "verdict": verdict,
    }
