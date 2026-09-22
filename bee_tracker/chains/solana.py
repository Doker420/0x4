"""Solana adapter."""
import logging
from typing import List

from .base import Chain, session

log = logging.getLogger("chains.sol")

SPL_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


class SolanaChain(Chain):
    name = "solana"
    symbol = "SOL"
    coingecko_id = "solana"
    family = "solana"
    explorer_tx = "https://solscan.io/tx/{hash}"

    RPC = "https://api.mainnet-beta.solana.com"

    async def _rpc(self, method: str, params: list):
        s = await session()
        payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
        async with s.post(self.RPC, json=payload) as r:
            data = await r.json(content_type=None)
        if "error" in data:
            raise RuntimeError(data["error"].get("message", "rpc error"))
        return data.get("result")

    async def get_balance(self, address: str) -> float:
        try:
            res = await self._rpc("getBalance", [address])
            return (res or {}).get("value", 0) / 1e9
        except Exception as e:
            log.debug("sol balance failed: %s", e)
            return 0.0

    async def get_token_balances(self, address: str, tokens=None) -> List[dict]:
        try:
            res = await self._rpc("getTokenAccountsByOwner", [
                address, {"programId": SPL_PROGRAM}, {"encoding": "jsonParsed"}
            ])
        except Exception:
            return []
        out = []
        for acc in (res or {}).get("value", []):
            try:
                info = acc["account"]["data"]["parsed"]["info"]
                amt = info["tokenAmount"]
                if float(amt["uiAmount"] or 0) > 0:
                    out.append({
                        "symbol": info["mint"][:6],
                        "address": info["mint"],
                        "balance": float(amt["uiAmount"]),
                        "coingecko_id": "",
                    })
            except Exception:
                continue
        return out

    async def get_txs(self, address: str, limit: int = 20) -> List[dict]:
        try:
            sigs = await self._rpc(
                "getSignaturesForAddress", [address, {"limit": limit}]
            )
        except Exception:
            return []
        # Signature list alone has no amounts; report as informational events.
        return [
            {"hash": s["signature"], "direction": "in", "amount": 0.0,
             "from": "", "to": address}
            for s in (sigs or []) if not s.get("err")
        ]
