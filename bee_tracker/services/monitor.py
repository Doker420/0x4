"""Background transaction monitor."""
import asyncio
import logging

from chains.registry import get_chain
from core.config import MONITOR_INTERVAL
from core.database import Database
from services.notify import notify_tx
from services.prices import get_prices

log = logging.getLogger("monitor")


class TxMonitor:
    def __init__(self, db: Database, interval: int = MONITOR_INTERVAL):
        self.db = db
        self.interval = interval
        self._seen: dict[str, set[str]] = {}
        self._sem = asyncio.Semaphore(10)
        self._running = False

    async def run(self):
        self._running = True
        log.info("monitor started (interval=%ss)", self.interval)
        while self._running:
            try:
                await self.tick()
            except Exception:
                log.exception("monitor tick failed")
            await asyncio.sleep(self.interval)

    def stop(self):
        self._running = False

    async def tick(self):
        watches = self.db.get_all_active_watches()
        if not watches:
            return
        await asyncio.gather(
            *[self._check(w) for w in watches], return_exceptions=True
        )

    async def _check(self, watch: dict):
        chain = get_chain(watch["chain"])
        if not chain:
            return
        async with self._sem:
            try:
                txs = await chain.get_txs(watch["address"], limit=20)
            except Exception as e:
                log.debug("get_txs failed %s: %s", watch["address"], e)
                return
        if not txs:
            return

        key = f"{watch['chain']}:{watch['address']}"
        first_run = key not in self._seen
        seen = self._seen.setdefault(key, set())

        if first_run:
            # Baseline only — never blast history on the first poll.
            seen.update(t["hash"] for t in txs)
            return

        price = (await get_prices([chain.coingecko_id])).get(chain.coingecko_id, 0.0)

        for t in txs:
            if t["hash"] in seen:
                continue
            seen.add(t["hash"])
            await self._handle(chain, watch, t, price)

        # Keep the dedup set from growing without bound.
        if len(seen) > 500:
            self._seen[key] = set(list(seen)[-250:])

    async def _handle(self, chain, watch: dict, t: dict, price: float):
        direction = t["direction"]
        wanted = watch.get("direction_filter", "both")
        if wanted in ("in", "out") and wanted != direction:
            return

        amount_usd = t["amount"] * price
        if amount_usd < (watch.get("min_amount_usd") or 0):
            return

        tx_id = self.db.add_tx(
            user_id=watch["user_id"], chain=watch["chain"], tx_hash=t["hash"],
            direction=direction, from_addr=t.get("from", ""),
            to_addr=t.get("to", ""), amount=t["amount"], token=chain.symbol,
            amount_usd=amount_usd,
        )
        if tx_id is None:
            return  # already seen in a previous process lifetime

        await notify_tx(self.db, watch["user_id"], {
            "chain": watch["chain"], "symbol": chain.symbol,
            "direction": direction, "amount": t["amount"],
            "amount_usd": amount_usd, "hash": t["hash"],
            "label": watch.get("label", ""),
        })
        self.db.mark_notified(tx_id)
        log.info("tx %s %s %.6f %s ($%.2f)", direction, watch["chain"],
                 t["amount"], chain.symbol, amount_usd)
