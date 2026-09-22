"""EVM chain adapter — read-only JSON-RPC calls."""
import logging
from typing import List

from .base import Chain, session

log = logging.getLogger("chains.evm")

BALANCE_OF = "0x70a08231"
TRANSFER_TOPIC = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)


class EVMChain(Chain):
    family = "evm"

    def __init__(self, name, rpc, symbol, coingecko_id, chain_id, explorer=""):
        self.name = name
        self.rpc = rpc
        self.symbol = symbol
        self.coingecko_id = coingecko_id
        self.chain_id = chain_id
        self.explorer_tx = explorer or "https://blockscan.com/tx/{hash}"

    async def _rpc(self, method: str, params: list):
        s = await session()
        payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
        async with s.post(self.rpc, json=payload) as r:
            data = await r.json(content_type=None)
        if "error" in data:
            raise RuntimeError(data["error"].get("message", "rpc error"))
        return data.get("result")

    async def get_balance(self, address: str) -> float:
        try:
            res = await self._rpc("eth_getBalance", [address, "latest"])
            return int(res, 16) / 1e18
        except Exception as e:
            log.debug("%s balance failed: %s", self.name, e)
            return 0.0

    async def get_token_balances(self, address: str, tokens=None) -> List[dict]:
        """Query balanceOf for an explicit token list (no indexer needed)."""
        out = []
        for t in tokens or []:
            try:
                data = BALANCE_OF + address.lower().removeprefix("0x").rjust(64, "0")
                res = await self._rpc(
                    "eth_call", [{"to": t["address"], "data": data}, "latest"]
                )
                raw = int(res, 16)
                if raw > 0:
                    out.append({
                        "symbol": t["symbol"],
                        "address": t["address"],
                        "balance": raw / (10 ** t["decimals"]),
                        "coingecko_id": t.get("coingecko_id", ""),
                    })
            except Exception:
                continue
        return out

    async def get_txs(self, address: str, limit: int = 20) -> List[dict]:
        """Scan recent blocks for native transfers touching `address`.

        Public RPCs rarely expose an address index, so we walk a short window
        of recent blocks. Explorer APIs can be layered on later per chain.
        """
        try:
            head = int(await self._rpc("eth_blockNumber", []), 16)
        except Exception:
            return []

        target = address.lower()
        out: List[dict] = []
        window = min(limit, 12)
        for n in range(head, max(head - window, 0), -1):
            try:
                block = await self._rpc("eth_getBlockByNumber", [hex(n), True])
            except Exception:
                continue
            if not block:
                continue
            for tx in block.get("transactions", []):
                frm = (tx.get("from") or "").lower()
                to = (tx.get("to") or "").lower()
                if target not in (frm, to):
                    continue
                value = int(tx.get("value", "0x0"), 16) / 1e18
                if value == 0:
                    continue
                out.append({
                    "hash": tx["hash"],
                    "direction": "in" if to == target else "out",
                    "amount": value,
                    "from": frm,
                    "to": to,
                })
                if len(out) >= limit:
                    return out
        return out
