"""Chain adapter interface."""
from abc import ABC, abstractmethod
from typing import List

import aiohttp

from core.config import HTTP_TIMEOUT

_session: aiohttp.ClientSession | None = None


async def session() -> aiohttp.ClientSession:
    """Shared aiohttp session — avoids opening a socket per RPC call."""
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
        )
    return _session


async def close_session():
    global _session
    if _session and not _session.closed:
        await _session.close()


class Chain(ABC):
    name: str = "unknown"
    symbol: str = "?"
    coingecko_id: str = ""
    family: str = "evm"
    explorer_tx: str = ""

    @abstractmethod
    async def get_balance(self, address: str) -> float:
        """Native coin balance, in whole units."""

    async def get_token_balances(self, address: str, tokens=None) -> List[dict]:
        return []

    async def get_txs(self, address: str, limit: int = 20) -> List[dict]:
        """Recent transactions: [{hash, direction, amount, from, to}]."""
        return []

    def tx_url(self, tx_hash: str) -> str:
        return self.explorer_tx.format(hash=tx_hash) if self.explorer_tx else ""
