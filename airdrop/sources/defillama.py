"""DefiLlama: protocols without a token / recently funded (classic airdrop candidates).

Public endpoints, no key required. Read-only.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

import httpx

from ..models import Airdrop
from .base import Source, clean_text, dict_get, get_json, guess_chain

log = logging.getLogger("airdrop.sources.defillama")

PROTOCOLS_URL = "https://api.llama.fi/protocols"
AIRDROPS_URL = "https://api.llama.fi/airdrops"
RAISES_URL = "https://api.llama.fi/raises"


class DefiLlamaSource(Source):
    name = "defillama"
    weight = 3.0

    async def fetch(self, client: httpx.AsyncClient) -> List[Airdrop]:
        items: List[Airdrop] = []
        items.extend(await self._airdrops(client))
        items.extend(await self._protocols(client))
        items.extend(await self._raises(client))
        return items

    # ------------------------------------------------------------------ #
    async def _airdrops(self, client: httpx.AsyncClient) -> List[Airdrop]:
        try:
            data = await get_json(client, AIRDROPS_URL)
        except Exception as exc:  # noqa: BLE001
            log.debug("defillama /airdrops unavailable: %s", exc)
            return []
        if not isinstance(data, list):
            return []
        out: List[Airdrop] = []
        for row in data[:60]:
            if not isinstance(row, dict):
                continue
            name = clean_text(dict_get(row, "name", "project", default=""), 80)
            if not name:
                continue
            page = dict_get(row, "page", "url", "link", default="https://defillama.fi/airdrops")
            text = f"{name} {clean_text(dict_get(row, 'description', 'notes', default=''), 200)}"
            out.append(
                Airdrop(
                    name=name,
                    url=page,
                    source=self.name,
                    chain=guess_chain(text) or dict_get(row, "chains", default="") if isinstance(dict_get(row, "chains", default=""), str) else guess_chain(text),
                    category="airdrop-listing",
                    summary=clean_text(dict_get(row, "description", "notes", default="")),
                    extra={"token": dict_get(row, "token", "tokenSymbol", default="")},
                )
            )
        return out

    async def _protocols(self, client: httpx.AsyncClient) -> List[Airdrop]:
        """Protocols with real TVL but no token yet are the classic retro candidates."""
        try:
            data = await get_json(client, PROTOCOLS_URL)
        except Exception as exc:  # noqa: BLE001
            log.debug("defillama /protocols unavailable: %s", exc)
            return []
        if not isinstance(data, list):
            return []
        out: List[Airdrop] = []
        for row in data:
            if not isinstance(row, dict):
                continue
            symbol = str(dict_get(row, "symbol", default="-") or "-").upper()
            if symbol not in {"-", "", "NONE", "NULL"}:
                continue  # already has a token
            tvl = float(dict_get(row, "tvl", default=0) or 0)
            if tvl < 5_000_000:
                continue
            name = clean_text(dict_get(row, "name", default=""), 80)
            if not name:
                continue
            chain = clean_text(dict_get(row, "chain", "chains", default=""), 40)
            if isinstance(chain, str) and "," in chain:
                chain = chain.split(",")[0]
            text = f"{name} {clean_text(dict_get(row, 'description', default=''), 200)} {chain}"
            out.append(
                Airdrop(
                    name=name,
                    url=dict_get(row, "url", default=f"https://defillama.fi/protocol/{clean_text(name, 40).lower()}"),
                    source=self.name,
                    chain=guess_chain(text),
                    category="no-token-protocol",
                    summary=clean_text(f"TVL ~${tvl:,.0f}. {dict_get(row, 'description', default='')}"),
                    extra={"tvl": tvl, "category": dict_get(row, "category", default="")},
                )
            )
        out.sort(key=lambda item: float(item.extra.get("tvl", 0)), reverse=True)
        return out[:40]

    async def _raises(self, client: httpx.AsyncClient) -> List[Airdrop]:
        """Recently funded projects very often ship a points season / retro drop."""
        try:
            data = await get_json(client, RAISES_URL)
        except Exception as exc:  # noqa: BLE001
            log.debug("defillama /raises unavailable: %s", exc)
            return []
        rows: List[Dict[str, Any]] = []
        if isinstance(data, dict):
            rows = list(data.get("raises") or [])[:50]
        elif isinstance(data, list):
            rows = list(data)[:50]
        out: List[Airdrop] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = clean_text(dict_get(row, "name", "project", default=""), 80)
            if not name:
                continue
            text = f"{name} raised {dict_get(row, 'amount', default='')} {clean_text(dict_get(row, 'notes', default=''), 150)}"
            out.append(
                Airdrop(
                    name=name,
                    url=dict_get(row, "url", "link", default="https://defillama.fi/raises"),
                    source=self.name,
                    chain=guess_chain(text),
                    category="funded-project",
                    summary=clean_text(text),
                    extra={"amount": dict_get(row, "amount", default=""), "round": dict_get(row, "round", default="")},
                )
            )
        return out
