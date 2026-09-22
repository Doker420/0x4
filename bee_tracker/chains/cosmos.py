"""Cosmos / interchain adapter via public REST (LCD) endpoints."""
import logging
from typing import List

from .base import Chain, session

log = logging.getLogger("chains.cosmos")


class CosmosChain(Chain):
    family = "cosmos"

    def __init__(self, name, rest, denom, decimals, coingecko_id, symbol=None):
        self.name = name
        self.rest = rest.rstrip("/")
        self.denom = denom
        self.decimals = decimals
        self.coingecko_id = coingecko_id
        self.symbol = symbol or denom.lstrip("u").upper()
        self.explorer_tx = f"https://www.mintscan.io/{name}/txs/{{hash}}"

    async def get_balance(self, address: str) -> float:
        try:
            s = await session()
            url = f"{self.rest}/cosmos/bank/v1beta1/balances/{address}"
            async with s.get(url) as r:
                j = await r.json(content_type=None)
            for b in j.get("balances", []):
                if b.get("denom") == self.denom:
                    return int(b["amount"]) / (10 ** self.decimals)
            return 0.0
        except Exception as e:
            log.debug("%s balance failed: %s", self.name, e)
            return 0.0


# (name, rest endpoint, denom, decimals, coingecko id)
COSMOS_CHAINS: List[tuple] = [
    ("cosmos", "https://rest.cosmos.directory/cosmoshub", "uatom", 6, "cosmos"),
    ("osmosis", "https://rest.cosmos.directory/osmosis", "uosmo", 6, "osmosis"),
    ("celestia", "https://rest.cosmos.directory/celestia", "utia", 6, "celestia"),
    ("sei", "https://rest.cosmos.directory/sei", "usei", 6, "sei-network"),
    ("dydx", "https://rest.cosmos.directory/dydx", "adydx", 18, "dydx-chain"),
    ("injective", "https://rest.cosmos.directory/injective", "inj", 18, "injective-protocol"),
    ("kujira", "https://rest.cosmos.directory/kujira", "ukuji", 6, "kujira"),
    ("juno", "https://rest.cosmos.directory/juno", "ujuno", 6, "juno-network"),
    ("stargaze", "https://rest.cosmos.directory/stargaze", "ustars", 6, "stargaze"),
    ("akash", "https://rest.cosmos.directory/akash", "uakt", 6, "akash-network"),
    ("axelar", "https://rest.cosmos.directory/axelar", "uaxl", 6, "axelar"),
    ("stride", "https://rest.cosmos.directory/stride", "ustrd", 6, "stride"),
    ("neutron", "https://rest.cosmos.directory/neutron", "untrn", 6, "neutron-3"),
    ("secret", "https://rest.cosmos.directory/secretnetwork", "uscrt", 6, "secret"),
    ("evmos", "https://rest.cosmos.directory/evmos", "aevmos", 18, "evmos"),
    ("persistence", "https://rest.cosmos.directory/persistence", "uxprt", 6, "persistence"),
    ("agoric", "https://rest.cosmos.directory/agoric", "ubld", 6, "agoric"),
    ("regen", "https://rest.cosmos.directory/regen", "uregen", 6, "regen"),
    ("sentinel", "https://rest.cosmos.directory/sentinel", "udvpn", 6, "sentinel"),
    ("comdex", "https://rest.cosmos.directory/comdex", "ucmdx", 6, "comdex"),
    ("chihuahua", "https://rest.cosmos.directory/chihuahua", "uhuahua", 6, "chihuahua-token"),
    ("bandchain", "https://rest.cosmos.directory/bandchain", "uband", 6, "band-protocol"),
    ("desmos", "https://rest.cosmos.directory/desmos", "udsm", 6, "desmos"),
    ("iris", "https://rest.cosmos.directory/irisnet", "uiris", 6, "iris-network"),
    ("kava", "https://rest.cosmos.directory/kava", "ukava", 6, "kava"),
    ("crescent", "https://rest.cosmos.directory/crescent", "ucre", 6, "crescent-network"),
    ("gravitybridge", "https://rest.cosmos.directory/gravitybridge", "ugraviton", 6, "graviton"),
    ("umee", "https://rest.cosmos.directory/umee", "uumee", 6, "umee"),
    ("quicksilver", "https://rest.cosmos.directory/quicksilver", "uqck", 6, "quicksilver"),
    ("mars", "https://rest.cosmos.directory/mars", "umars", 6, "mars-protocol-a7fcbcfb-fd61-4017-92f0-7ee9f9cc6da3"),
]
