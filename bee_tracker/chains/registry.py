"""Registry of all supported chains."""
from typing import Dict, List, Optional

from chains.base import Chain
from chains.bitcoin import BitcoinChain
from chains.cosmos import COSMOS_CHAINS, CosmosChain
from chains.evm import EVMChain
from chains.solana import SolanaChain
from chains.ton import TonChain
from core.config import EVM_RPCS

# name -> (symbol, coingecko id, chain id, explorer tx url)
EVM_META = {
    "ethereum": ("ETH", "ethereum", 1, "https://etherscan.io/tx/{hash}"),
    "bsc": ("BNB", "binancecoin", 56, "https://bscscan.com/tx/{hash}"),
    "polygon": ("POL", "matic-network", 137, "https://polygonscan.com/tx/{hash}"),
    "arbitrum": ("ETH", "ethereum", 42161, "https://arbiscan.io/tx/{hash}"),
    "optimism": ("ETH", "ethereum", 10, "https://optimistic.etherscan.io/tx/{hash}"),
    "base": ("ETH", "ethereum", 8453, "https://basescan.org/tx/{hash}"),
    "avalanche": ("AVAX", "avalanche-2", 43114, "https://snowtrace.io/tx/{hash}"),
    "fantom": ("FTM", "fantom", 250, "https://ftmscan.com/tx/{hash}"),
    "gnosis": ("XDAI", "xdai", 100, "https://gnosisscan.io/tx/{hash}"),
    "celo": ("CELO", "celo", 42220, "https://celoscan.io/tx/{hash}"),
    "moonbeam": ("GLMR", "moonbeam", 1284, "https://moonscan.io/tx/{hash}"),
    "moonriver": ("MOVR", "moonriver", 1285, "https://moonriver.moonscan.io/tx/{hash}"),
    "aurora": ("ETH", "ethereum", 1313161554, "https://explorer.aurora.dev/tx/{hash}"),
    "harmony": ("ONE", "harmony", 1666600000, "https://explorer.harmony.one/tx/{hash}"),
    "cronos": ("CRO", "crypto-com-chain", 25, "https://cronoscan.com/tx/{hash}"),
    "zksync": ("ETH", "ethereum", 324, "https://explorer.zksync.io/tx/{hash}"),
    "linea": ("ETH", "ethereum", 59144, "https://lineascan.build/tx/{hash}"),
    "scroll": ("ETH", "ethereum", 534352, "https://scrollscan.com/tx/{hash}"),
    "mantle": ("MNT", "mantle", 5000, "https://explorer.mantle.xyz/tx/{hash}"),
    "blast": ("ETH", "ethereum", 81457, "https://blastscan.io/tx/{hash}"),
    "metis": ("METIS", "metis-token", 1088, "https://explorer.metis.io/tx/{hash}"),
    "boba": ("ETH", "ethereum", 288, "https://bobascan.com/tx/{hash}"),
    "kava": ("KAVA", "kava", 2222, "https://kavascan.com/tx/{hash}"),
    "canto": ("CANTO", "canto", 7700, "https://tuber.build/tx/{hash}"),
    "core": ("CORE", "coredaoorg", 1116, "https://scan.coredao.org/tx/{hash}"),
    "opbnb": ("BNB", "binancecoin", 204, "https://opbnbscan.com/tx/{hash}"),
    "polygonzkevm": ("ETH", "ethereum", 1101, "https://zkevm.polygonscan.com/tx/{hash}"),
    "mode": ("ETH", "ethereum", 34443, "https://explorer.mode.network/tx/{hash}"),
    "fraxtal": ("FRAX", "frax-share", 252, "https://fraxscan.com/tx/{hash}"),
    "zora": ("ETH", "ethereum", 7777777, "https://explorer.zora.energy/tx/{hash}"),
}

CHAINS: Dict[str, Chain] = {}


def _init():
    if CHAINS:
        return
    for name, rpc in EVM_RPCS.items():
        meta = EVM_META.get(name)
        if not meta:
            continue
        sym, cg, cid, expl = meta
        CHAINS[name] = EVMChain(name, rpc, sym, cg, cid, expl)

    CHAINS["bitcoin"] = BitcoinChain()
    CHAINS["solana"] = SolanaChain()
    CHAINS["ton"] = TonChain()

    for name, rest, denom, dec, cg in COSMOS_CHAINS:
        CHAINS[name] = CosmosChain(name, rest, denom, dec, cg)


def get_chain(name: str) -> Optional[Chain]:
    _init()
    return CHAINS.get((name or "").lower())


def list_chains() -> List[str]:
    _init()
    return sorted(CHAINS)


def chains_by_family() -> Dict[str, List[str]]:
    _init()
    out: Dict[str, List[str]] = {}
    for name, ch in CHAINS.items():
        out.setdefault(ch.family, []).append(name)
    return {k: sorted(v) for k, v in out.items()}


def total_chains() -> int:
    _init()
    return len(CHAINS)


def family_of(name: str) -> str:
    ch = get_chain(name)
    return ch.family if ch else "evm"
