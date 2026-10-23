"""Shared plumbing for discovery sources."""

from __future__ import annotations

import asyncio
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx

from ..models import Airdrop
from ..settings import settings

log = logging.getLogger("airdrop.sources")

USER_AGENT = settings.user_agent


@dataclass
class FetchOutcome:
    source: str
    items: List[Airdrop] = None  # type: ignore[assignment]
    error: str = ""

    def __post_init__(self) -> None:
        if self.items is None:
            self.items = []


class Source(ABC):
    """A public, read-only source of airdrop candidates."""

    name = "base"
    weight = 1.0

    @abstractmethod
    async def fetch(self, client: httpx.AsyncClient) -> List[Airdrop]:
        ...

    async def safe_fetch(self, client: httpx.AsyncClient) -> FetchOutcome:
        try:
            items = await asyncio.wait_for(self.fetch(client), timeout=settings.request_timeout + 10)
            return FetchOutcome(source=self.name, items=items or [])
        except asyncio.TimeoutError:
            return FetchOutcome(source=self.name, error="timeout")
        except Exception as exc:  # noqa: BLE001 - one broken source must not kill the run
            log.warning("source %s failed: %s", self.name, exc)
            return FetchOutcome(source=self.name, error=str(exc)[:300])


async def get_json(client: httpx.AsyncClient, url: str, **kwargs: Any) -> Any:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    headers.update(kwargs.pop("headers", None) or {})
    response = await client.get(url, headers=headers, **kwargs)
    response.raise_for_status()
    return response.json()


async def get_text(client: httpx.AsyncClient, url: str, **kwargs: Any) -> str:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml, text/xml, */*"}
    headers.update(kwargs.pop("headers", None) or {})
    response = await client.get(url, headers=headers, **kwargs)
    response.raise_for_status()
    return response.text


def _ssl_verify():
    """Trust the system CA store (respects SSL_CERT_FILE / AIRDROP_CA_BUNDLE).

    Some hosts (and corporate proxies) ship their own root certificate that is
    not in certifi's bundle; using the system store keeps those setups working.
    """
    import ssl

    bundle = os.getenv("AIRDROP_CA_BUNDLE", "").strip()
    if bundle:
        return bundle
    try:
        return ssl.create_default_context()
    except Exception:  # pragma: no cover
        return True


def new_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=settings.request_timeout,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
        verify=_ssl_verify(),
    )


def clean_text(value: Optional[str], limit: int = 400) -> str:
    if not value:
        return ""
    text = " ".join(str(value).split())
    return text[:limit]


def host_of(url: str) -> str:
    from urllib.parse import urlparse

    try:
        return urlparse(url).netloc.lower().replace("www.", "")
    except Exception:  # noqa: BLE001
        return ""


def guess_chain(text: str) -> str:
    lowered = (text or "").lower()
    table = {
        "ethereum": "ethereum",
        " mainnet": "ethereum",
        "arbitrum": "arbitrum",
        "base chain": "base",
        "on base": "base",
        "optimism": "optimism",
        "polygon": "polygon",
        "bsc": "bsc",
        "bnb chain": "bsc",
        "linea": "linea",
        "scroll": "scroll",
        "blast": "blast",
        "zksync": "zksync",
        "solana": "solana",
    }
    for needle, chain in table.items():
        if needle in lowered:
            return chain
    return ""


def dict_get(data: Dict[str, Any], *keys: str, default: Any = "") -> Any:
    for key in keys:
        if isinstance(data, dict) and data.get(key) not in (None, ""):
            return data[key]
    return default
