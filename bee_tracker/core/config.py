"""Configuration for BEE Tracker.

SECURITY MODEL: this project is strictly watch-only. It accepts public
addresses and extended PUBLIC keys (xpub/ypub/zpub) only. It never accepts,
transmits, stores or derives from seed phrases or private keys, and therefore
holds no material capable of moving funds.
"""
import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = [
    int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x
]

API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "5050"))

DB_PATH = os.getenv("DB_PATH", "bee_tracker.db")

COINGECKO_API = "https://api.coingecko.com/api/v3"
PRICE_TTL = int(os.getenv("PRICE_TTL", "60"))
MONITOR_INTERVAL = int(os.getenv("MONITOR_INTERVAL", "120"))

# How many addresses to derive per xpub account by default, and the hard cap.
XPUB_DEFAULT_GAP = int(os.getenv("XPUB_DEFAULT_GAP", "20"))
XPUB_MAX_GAP = int(os.getenv("XPUB_MAX_GAP", "200"))

HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "15"))

# ─── Tariff limits ──────────────────────────────────────────────────────────
# "team" is sized for an internal treasury desk of 5-10 people tracking many
# corporate wallets, so its ceilings are deliberately high (effectively
# unlimited). -1 means no limit.
TARIFF_LIMITS: dict[str, dict[str, int]] = {
    "free": {"wallets": 3, "watches": 3, "xpubs": 1, "webhooks": 0, "gap": 20},
    "pro": {"wallets": 50, "watches": 100, "xpubs": 10, "webhooks": 5, "gap": 50},
    "team": {"wallets": -1, "watches": -1, "xpubs": -1, "webhooks": 50, "gap": 200},
}


def limit_for(tariff: str, key: str) -> int:
    return TARIFF_LIMITS.get(tariff, TARIFF_LIMITS["free"]).get(key, 0)


EVM_RPCS: dict[str, str] = {
    "ethereum": "https://eth.llamarpc.com",
    "bsc": "https://bsc-dataseed.binance.org",
    "polygon": "https://polygon-rpc.com",
    "arbitrum": "https://arb1.arbitrum.io/rpc",
    "optimism": "https://mainnet.optimism.io",
    "base": "https://mainnet.base.org",
    "avalanche": "https://api.avax.network/ext/bc/C/rpc",
    "fantom": "https://rpc.ftm.tools",
    "gnosis": "https://rpc.gnosischain.com",
    "celo": "https://forno.celo.org",
    "moonbeam": "https://rpc.api.moonbeam.network",
    "moonriver": "https://rpc.api.moonriver.moonbeam.network",
    "aurora": "https://mainnet.aurora.dev",
    "harmony": "https://api.harmony.one",
    "cronos": "https://evm.cronos.org",
    "zksync": "https://mainnet.era.zksync.io",
    "linea": "https://rpc.linea.build",
    "scroll": "https://rpc.scroll.io",
    "mantle": "https://rpc.mantle.xyz",
    "blast": "https://rpc.blast.io",
    "metis": "https://andromeda.metis.io/?owner=1088",
    "boba": "https://mainnet.boba.network",
    "kava": "https://evm.kava.io",
    "canto": "https://canto.slingshot.finance",
    "core": "https://rpc.coredao.org",
    "opbnb": "https://opbnb-mainnet-rpc.bnbchain.org",
    "polygonzkevm": "https://zkevm-rpc.com",
    "mode": "https://mainnet.mode.network",
    "fraxtal": "https://rpc.frax.com",
    "zora": "https://rpc.zora.energy",
}
