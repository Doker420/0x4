"""TON adapter via toncenter."""
import logging
import os
from typing import List

from .base import Chain, session

log = logging.getLogger("chains.ton")


class TonChain(Chain):
    name = "ton"
    symbol = "TON"
    coingecko_id = "the-open-network"
    family = "ton"
    explorer_tx = "https://tonviewer.com/transaction/{hash}"

    API = "https://toncenter.com/api/v2"

    def _headers(self):
        key = os.getenv("TONCENTER_API_KEY", "")
        return {"X-API-Key": key} if key else {}

    async def get_balance(self, address: str) -> float:
        try:
            s = await session()
            async with s.get(
                f"{self.API}/getAddressBalance",
                params={"address": address},
                headers=self._headers(),
            ) as r:
                j = await r.json(content_type=None)
            return int(j.get("result", 0)) / 1e9
        except Exception as e:
            log.debug("ton balance failed: %s", e)
            return 0.0

    async def get_txs(self, address: str, limit: int = 20) -> List[dict]:
        try:
            s = await session()
            async with s.get(
                f"{self.API}/getTransactions",
                params={"address": address, "limit": limit},
                headers=self._headers(),
            ) as r:
                j = await r.json(content_type=None)
        except Exception:
            return []

        out = []
        for t in j.get("result", []):
            in_msg = t.get("in_msg") or {}
            value = int(in_msg.get("value", 0) or 0) / 1e9
            tx_hash = t.get("transaction_id", {}).get("hash", "")
            if value > 0:
                out.append({
                    "hash": tx_hash, "direction": "in", "amount": value,
                    "from": in_msg.get("source", ""), "to": address,
                })
                continue
            for om in t.get("out_msgs", []):
                ov = int(om.get("value", 0) or 0) / 1e9
                if ov > 0:
                    out.append({
                        "hash": tx_hash, "direction": "out", "amount": ov,
                        "from": address, "to": om.get("destination", ""),
                    })
        return out
