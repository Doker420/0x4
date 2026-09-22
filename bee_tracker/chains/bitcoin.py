"""Bitcoin adapter (legacy, segwit, taproot) via Blockstream/Esplora."""
import logging
from typing import List

from .base import Chain, session

log = logging.getLogger("chains.btc")


class BitcoinChain(Chain):
    name = "bitcoin"
    symbol = "BTC"
    coingecko_id = "bitcoin"
    family = "bitcoin"
    explorer_tx = "https://blockstream.info/tx/{hash}"

    def __init__(self, explorer: str = "https://blockstream.info/api"):
        self.explorer = explorer

    async def get_balance(self, address: str) -> float:
        try:
            s = await session()
            async with s.get(f"{self.explorer}/address/{address}") as r:
                j = await r.json(content_type=None)
            st = j.get("chain_stats", {})
            return (st.get("funded_txo_sum", 0) - st.get("spent_txo_sum", 0)) / 1e8
        except Exception as e:
            log.debug("btc balance failed: %s", e)
            return 0.0

    async def get_txs(self, address: str, limit: int = 20) -> List[dict]:
        try:
            s = await session()
            async with s.get(f"{self.explorer}/address/{address}/txs") as r:
                txs = await r.json(content_type=None)
        except Exception:
            return []

        out = []
        for tx in (txs or [])[:limit]:
            received = sum(
                v.get("value", 0) for v in tx.get("vout", [])
                if v.get("scriptpubkey_address") == address
            )
            sent = sum(
                v.get("prevout", {}).get("value", 0) for v in tx.get("vin", [])
                if v.get("prevout", {}).get("scriptpubkey_address") == address
            )
            net = received - sent
            if net == 0:
                continue
            out.append({
                "hash": tx["txid"],
                "direction": "in" if net > 0 else "out",
                "amount": abs(net) / 1e8,
                "from": "",
                "to": address,
            })
        return out
