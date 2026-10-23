"""Runtime configuration for the airdrop agent (env-driven, no secrets in code)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

try:  # python-dotenv is optional at import time
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    def load_dotenv(*_args, **_kwargs):  # type: ignore
        return False


ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=False)


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name, "")
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on", "y"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(_env(name, str(default))))
    except ValueError:
        return default


def _env_list(name: str, default: str = "") -> List[str]:
    return [part.strip() for part in _env(name, default).split(",") if part.strip()]


def _admin_ids(*names: str) -> List[int]:
    ids: List[int] = []
    for name in names:
        for part in _env(name, "").split(","):
            part = part.strip()
            if part.isdigit():
                value = int(part)
                if value not in ids:
                    ids.append(value)
    return ids


# Public RPC fallbacks. Override any of them with RPC_<CHAIN> in .env.
DEFAULT_RPC: Dict[str, str] = {
    "ethereum": "https://ethereum-rpc.publicnode.com",
    "arbitrum": "https://arbitrum-one-rpc.publicnode.com",
    "base": "https://base-rpc.publicnode.com",
    "optimism": "https://optimism-rpc.publicnode.com",
    "polygon": "https://polygon-bor-rpc.publicnode.com",
    "bsc": "https://bsc-rpc.publicnode.com",
    "linea": "https://linea-rpc.publicnode.com",
    "scroll": "https://scroll-rpc.publicnode.com",
    "blast": "https://blast-rpc.publicnode.com",
    "zksync": "https://mainnet.era.zksync.io",
}

CHAIN_IDS: Dict[str, int] = {
    "ethereum": 1,
    "arbitrum": 42161,
    "base": 8453,
    "optimism": 10,
    "polygon": 137,
    "bsc": 56,
    "linea": 59144,
    "scroll": 534352,
    "blast": 81457,
    "zksync": 324,
}

# Minimal ERC-20 ABI fragment used by balance/allowance reads and claim calls.
ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "symbol",
        "outputs": [{"name": "", "type": "string"}],
        "type": "function",
    },
]

# claim(bytes32[] proof,uint256 amount) / claim(uint256 amount) / claim() variants.
CLAIM_ABI = [
    {
        "inputs": [],
        "name": "claim",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"name": "amount", "type": "uint256"}],
        "name": "claim",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "index", "type": "uint256"},
            {"name": "account", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "merkleProof", "type": "bytes32[]"},
        ],
        "name": "claim",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"name": "proof", "type": "bytes32[]"}, {"name": "amount", "type": "uint256"}],
        "name": "claim",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [{"name": "index", "type": "uint256"}],
        "name": "isClaimed",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
]


@dataclass
class Settings:
    """Typed view over the environment. Safe defaults: nothing is spent by default."""

    root: Path = ROOT
    data_dir: Path = field(default_factory=lambda: Path(_env("AIRDROP_DATA_DIR", str(ROOT / "data" / "airdrop"))))

    # Money safety -------------------------------------------------------
    dry_run: bool = field(default_factory=lambda: _env_bool("AIRDROP_DRY_RUN", True))
    max_tx_value_eth: float = field(default_factory=lambda: _env_float("AIRDROP_MAX_TX_VALUE_ETH", 0.01))
    daily_limit_eth: float = field(default_factory=lambda: _env_float("AIRDROP_DAILY_LIMIT_ETH", 0.05))
    max_gas_gwei: float = field(default_factory=lambda: _env_float("AIRDROP_MAX_GAS_GWEI", 25.0))
    confirm_above_eth: float = field(default_factory=lambda: _env_float("AIRDROP_CONFIRM_ABOVE_ETH", 0.005))
    allow_unknown_contracts: bool = field(default_factory=lambda: _env_bool("AIRDROP_ALLOW_UNKNOWN_CONTRACTS", False))

    # Scheduling ---------------------------------------------------------
    discovery_interval_min: int = field(default_factory=lambda: _env_int("AIRDROP_DISCOVERY_MIN", 90))
    eligibility_interval_min: int = field(default_factory=lambda: _env_int("AIRDROP_ELIGIBILITY_MIN", 360))
    notify_score_threshold: float = field(default_factory=lambda: _env_float("AIRDROP_NOTIFY_SCORE", 6.0))
    request_timeout: float = field(default_factory=lambda: _env_float("AIRDROP_HTTP_TIMEOUT", 20.0))
    user_agent: str = field(default_factory=lambda: _env("AIRDROP_USER_AGENT", "0x4-airdrop-agent/1.0 (+self-hosted)"))

    # Telegram -----------------------------------------------------------
    bot_token: str = field(default_factory=lambda: _env("AIRDROP_BOT_TOKEN", ""))
    admin_ids: List[int] = field(default_factory=lambda: _admin_ids("AIRDROP_ADMIN_IDS", "ADMIN_IDS"))

    # Discovery ----------------------------------------------------------
    enabled_sources: List[str] = field(
        default_factory=lambda: _env_list(
            "AIRDROP_SOURCES", "defillama,coingecko,rss,github,manual"
        )
    )
    rss_feeds: List[str] = field(
        default_factory=lambda: _env_list(
            "AIRDROP_RSS_FEEDS",
            "https://cointelegraph.com/rss,"
            "https://decrypt.co/feed,"
            "https://www.theblock.co/rss.xml,"
            "https://coinjournal.net/feed/",
        )
    )
    github_queries: List[str] = field(
        default_factory=lambda: _env_list(
            "AIRDROP_GITHUB_QUERIES",
            "airdrop+tracker,airdrop+list,crypto+airdrop",
        )
    )
    watch_keywords: List[str] = field(
        default_factory=lambda: _env_list(
            "AIRDROP_KEYWORDS",
            "airdrop,air drop,retro,retroactive,token generation,tge,snapshot,claim,snapshot taken,"
            "points season,eligibility,token launch,whitelist,testnet rewards",
        )
    )

    # Chains -------------------------------------------------------------
    default_chain: str = field(default_factory=lambda: _env("AIRDROP_DEFAULT_CHAIN", "base"))
    rpc: Dict[str, str] = field(default_factory=dict)

    # Paths --------------------------------------------------------------
    # Overridable (tests and multi-instance installs point these elsewhere).
    _db_path: Optional[Path] = None
    _keystore_dir: Optional[Path] = None
    _log_path: Optional[Path] = None
    _projects_file: Optional[Path] = None

    @property
    def db_path(self) -> Path:
        return self._db_path or self.data_dir / "airdrop.sqlite3"

    @db_path.setter
    def db_path(self, value: Path) -> None:
        self._db_path = Path(value)

    @property
    def keystore_dir(self) -> Path:
        return self._keystore_dir or self.data_dir / "keystore"

    @keystore_dir.setter
    def keystore_dir(self, value: Path) -> None:
        self._keystore_dir = Path(value)

    @property
    def log_path(self) -> Path:
        return self._log_path or self.data_dir / "agent.log"

    @log_path.setter
    def log_path(self, value: Path) -> None:
        self._log_path = Path(value)

    @property
    def projects_file(self) -> Path:
        """Optional operator-maintained list of projects to watch/claim."""
        return self._projects_file or Path(_env("AIRDROP_PROJECTS_FILE", str(ROOT / "config" / "airdrops.json")))

    @projects_file.setter
    def projects_file(self, value: Path) -> None:
        self._projects_file = Path(value)

    def rpc_url(self, chain: str) -> str:
        if chain in self.rpc:
            return self.rpc[chain]
        override = _env(f"RPC_{chain.upper()}", "")
        url = override or DEFAULT_RPC.get(chain, "")
        if url:
            self.rpc[chain] = url
        return url

    def chain_id(self, chain: str) -> int:
        return CHAIN_IDS.get(chain, 0)

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.keystore_dir.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings


settings = load_settings()
