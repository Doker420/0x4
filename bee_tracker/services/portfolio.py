"""Portfolio aggregation across all tracked wallets."""
import asyncio
import csv
import io
import logging
from typing import Optional

from chains.registry import get_chain
from core.database import Database
from services.prices import get_prices

log = logging.getLogger("portfolio")

# Bound concurrency so we do not hammer public RPC endpoints.
_sem = asyncio.Semaphore(20)


async def _wallet_balance(w: dict) -> dict:
    chain = get_chain(w["chain"])
    if not chain:
        return {**w, "error": f"unknown chain {w['chain']}", "native_balance": 0.0}
    async with _sem:
        try:
            bal = await chain.get_balance(w["address"])
        except Exception as e:
            log.debug("balance error %s: %s", w["address"], e)
            return {**w, "error": str(e), "native_balance": 0.0}
    return {
        "id": w["id"],
        "label": w["label"],
        "chain": w["chain"],
        "address": w["address"],
        "source": w.get("source", "manual"),
        "derivation_path": w.get("derivation_path"),
        "symbol": chain.symbol,
        "coingecko_id": chain.coingecko_id,
        "native_balance": bal,
    }


async def build_portfolio(
    db: Database, user_id: int, snapshot: bool = False, hide_empty: bool = True
) -> dict:
    """Aggregate every wallet of a user into a single USD-denominated view."""
    wallets = db.get_wallets(user_id)
    if not wallets:
        return {"total_usd": 0.0, "wallets": [], "by_chain": {}, "count": 0}

    results = await asyncio.gather(
        *[_wallet_balance(w) for w in wallets], return_exceptions=False
    )

    prices = await get_prices([r.get("coingecko_id", "") for r in results])

    total = 0.0
    by_chain: dict[str, float] = {}
    enriched = []
    for r in results:
        if r.get("error"):
            enriched.append(r)
            continue
        price = prices.get(r["coingecko_id"], 0.0)
        r["native_price"] = price
        r["native_usd"] = r["native_balance"] * price
        total += r["native_usd"]
        by_chain[r["chain"]] = by_chain.get(r["chain"], 0.0) + r["native_usd"]

        if snapshot:
            db.save_snapshot(user_id, r["id"], r["chain"], r["symbol"],
                             r["native_balance"], r["native_usd"])
        if hide_empty and r["native_balance"] == 0:
            continue
        enriched.append(r)

    enriched.sort(key=lambda x: x.get("native_usd", 0), reverse=True)
    return {
        "total_usd": total,
        "wallets": enriched,
        "by_chain": dict(sorted(by_chain.items(), key=lambda kv: -kv[1])),
        "count": len(wallets),
    }


def portfolio_to_csv(portfolio: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["label", "chain", "address", "source", "derivation_path",
                "symbol", "balance", "price_usd", "value_usd"])
    for x in portfolio["wallets"]:
        if x.get("error"):
            continue
        w.writerow([
            x.get("label", ""), x["chain"], x["address"], x.get("source", ""),
            x.get("derivation_path") or "", x.get("symbol", ""),
            f"{x.get('native_balance', 0):.8f}",
            f"{x.get('native_price', 0):.6f}",
            f"{x.get('native_usd', 0):.2f}",
        ])
    w.writerow([])
    w.writerow(["TOTAL", "", "", "", "", "", "", "", f"{portfolio['total_usd']:.2f}"])
    return buf.getvalue()


async def sync_xpub(
    db: Database, user_id: int, xpub_id: int, gap: Optional[int] = None
) -> int:
    """Derive addresses from a stored xpub and register them as wallets."""
    from core.derive import derive_addresses

    rows = [x for x in db.get_xpubs(user_id) if x["id"] == xpub_id]
    if not rows:
        return 0
    x = rows[0]
    count = gap or x["gap"]
    derived = derive_addresses(x["xpub"], x["chain"], count=count)
    return db.add_wallets_bulk(user_id, [
        {
            "label": f"{x['label']} #{d['index']}",
            "chain": x["chain"],
            "address": d["address"],
            "source": "xpub",
            "xpub_id": xpub_id,
            "derivation_path": d["path"],
        }
        for d in derived
    ])
