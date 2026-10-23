"""CoinGecko: newly listed / trending tokens — many retro drops are announced around TGE."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

import httpx

from ..models import Airdrop
from .base import Source, clean_text, dict_get, get_json, guess_chain

log = logging.getLogger("airdrop.sources.coingecko")

TRENDING_URL = "https://api.coingecko.com/api/v3/search/trending"
MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"


class CoinGeckoSource(Source):
    name = "coingecko"
    weight = 1.5

    async def fetch(self, client: httpx.AsyncClient) -> List[Airdrop]:
        items: List[Airdrop] = []
        items.extend(await self._trending(client))
        items.extend(await self._new_listings(client))
        return items

    async def _trending(self, client: httpx.AsyncClient) -> List[Airdrop]:
        try:
            data = await get_json(client, TRENDING_URL)
        except Exception as exc:  # noqa: BLE001
            log.debug("coingecko trending unavailable: %s", exc)
            return []
        coins = (data or {}).get("coins") or []
        out: List[Airdrop] = []
        for entry in coins[:15]:
            item = (entry or {}).get("item") or {}
            name = clean_text(dict_get(item, "name", default=""), 60)
            symbol = clean_text(dict_get(item, "symbol", default=""), 12)
            if not name:
                continue
            out.append(
                Airdrop(
                    name=f"{name} ({symbol})" if symbol else name,
                    url=f"https://www.coingecko.com/en/coins/{dict_get(item, 'id', default='')}",
                    source=self.name,
                    category="trending",
                    summary=clean_text(f"Трендовый токен #{dict_get(item, 'market_cap_rank', default='?')} в CoinGecko."),
                    extra={"symbol": symbol, "id": dict_get(item, "id", default="")},
                )
            )
        return out

    async def _new_listings(self, client: httpx.AsyncClient) -> List[Airdrop]:
        params = {
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": 100,
            "page": 1,
            "price_change_percentage": "24h",
        }
        try:
            data = await get_json(client, MARKETS_URL, params=params)
        except Exception as exc:  # noqa: BLE001
            log.debug("coingecko markets unavailable: %s", exc)
            return []
        if not isinstance(data, list):
            return []
        out: List[Airdrop] = []
        for row in data:
            if not isinstance(row, dict):
                continue
            # Tokens listed recently with a tiny market cap often still have a
            # claim window open for early users.
            change = float(dict_get(row, "price_change_percentage_24h", default=0) or 0)
            if abs(change) < 12:
                continue
            name = clean_text(dict_get(row, "name", default=""), 60)
            symbol = clean_text(dict_get(row, "symbol", default=""), 12).upper()
            if not name:
                continue
            out.append(
                Airdrop(
                    name=f"{name} ({symbol})" if symbol else name,
                    url=f"https://www.coingecko.com/en/coins/{dict_get(row, 'id', default='')}",
                    source=self.name,
                    category="fresh-listing",
                    summary=clean_text(
                        f"{symbol}: {change:+.1f}% за 24ч, капа ${float(dict_get(row, 'market_cap', default=0) or 0):,.0f}."
                    ),
                    extra={"symbol": symbol, "change24h": change},
                )
            )
        return out[:25]
