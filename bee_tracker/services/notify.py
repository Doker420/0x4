"""Telegram + webhook delivery."""
import hashlib
import hmac
import json
import logging
import time

from chains.base import session
from chains.registry import get_chain
from core.database import Database
from core.security import validate_webhook_url

log = logging.getLogger("notify")

_bot = None


def set_bot(bot):
    """Injected by bot.py at startup to avoid a circular import."""
    global _bot
    _bot = bot


def format_tx(tx: dict) -> str:
    arrow = "⬇️" if tx["direction"] == "in" else "⬆️"
    word = "входящая" if tx["direction"] == "in" else "исходящая"
    chain = get_chain(tx["chain"])
    url = chain.tx_url(tx["hash"]) if chain else ""
    link = f"\n\n<a href='{url}'>Открыть в эксплорере</a>" if url else ""
    return (
        f"{arrow} <b>Транзакция</b> — {word}\n\n"
        f"<b>Кошелёк:</b> {tx.get('label', '—')}\n"
        f"<b>Сеть:</b> {tx['chain']}\n"
        f"<b>Сумма:</b> {tx['amount']:.6f} {tx['symbol']}\n"
        f"<b>В USD:</b> ${tx['amount_usd']:,.2f}\n"
        f"<code>{tx['hash'][:24]}…</code>{link}"
    )


async def send_telegram(user_id: int, text: str) -> bool:
    if _bot is None:
        log.debug("no bot bound; skipping telegram notify")
        return False
    try:
        await _bot.send_message(user_id, text, disable_web_page_preview=True)
        return True
    except Exception as e:
        log.warning("telegram notify failed for %s: %s", user_id, e)
        return False


async def send_webhooks(db: Database, user_id: int, event: str, payload: dict):
    """POST a signed payload to every subscribed webhook."""
    for wh in db.get_webhooks(user_id):
        if not wh["active"]:
            continue
        if event not in [e.strip() for e in (wh["events"] or "").split(",")]:
            continue
        try:
            url = validate_webhook_url(wh["url"])
        except Exception as e:
            log.warning("skipping invalid webhook %s: %s", wh["id"], e)
            continue

        body = json.dumps(
            {"event": event, "ts": int(time.time()), "data": payload},
            ensure_ascii=False, separators=(",", ":"),
        )
        sig = hmac.new(
            wh["secret"].encode(), body.encode(), hashlib.sha256
        ).hexdigest()
        try:
            s = await session()
            async with s.post(
                url,
                data=body.encode(),
                headers={
                    "Content-Type": "application/json",
                    "X-Bee-Event": event,
                    "X-Bee-Signature": f"sha256={sig}",
                },
            ) as r:
                log.info("webhook %s -> %s", wh["id"], r.status)
        except Exception as e:
            log.warning("webhook %s failed: %s", wh["id"], e)


async def notify_tx(db: Database, user_id: int, tx: dict):
    user = db.get_user(user_id)
    if user and user.get("notify_on_tx"):
        await send_telegram(user_id, format_tx(tx))
    await send_webhooks(
        db, user_id, f"tx_{tx['direction']}",
        {k: tx[k] for k in
         ("chain", "symbol", "direction", "amount", "amount_usd", "hash", "label")
         if k in tx},
    )
