"""CoinGecko price lookups with a short-lived cache."""
import asyncio
import logging
import time
from typing import Dict, List

from chains.base import session
from core.config import COINGECKO_API, PRICE_TTL

log = logging.getLogger("prices")

_cache: Dict[str, tuple[float, float]] = {}
_lock = asyncio.Lock()


async def get_prices(coingecko_ids: List[str]) -> Dict[str, float]:
    """Return {coingecko_id: usd_price}, cached for PRICE_TTL seconds."""
    ids = {c for c in coingecko_ids if c}
    if not ids:
        return {}

    now = time.time()
    result: Dict[str, float] = {}
    missing: List[str] = []
    for cid in ids:
        hit = _cache.get(cid)
        if hit and hit[1] > now:
            result[cid] = hit[0]
        else:
            missing.append(cid)

    if not missing:
        return result

    async with _lock:
        # Batch in chunks so the URL stays within CoinGecko's limits.
        for i in range(0, len(missing), 100):
            chunk = missing[i:i + 100]
            try:
                s = await session()
                async with s.get(
                    f"{COINGECKO_API}/simple/price",
                    params={"ids": ",".join(chunk), "vs_currencies": "usd"},
                ) as r:
                    if r.status == 429:
                        log.warning("coingecko rate limited")
                        continue
                    data = await r.json(content_type=None)
                for cid, obj in (data or {}).items():
                    price = float(obj.get("usd", 0) or 0)
                    result[cid] = price
                    _cache[cid] = (price, now + PRICE_TTL)
            except Exception as e:
                log.warning("price fetch failed: %s", e)

    for cid in missing:
        result.setdefault(cid, 0.0)
    return result


async def get_price(coingecko_id: str) -> float:
    return (await get_prices([coingecko_id])).get(coingecko_id, 0.0)
