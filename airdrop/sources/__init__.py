"""Discovery source registry."""

from __future__ import annotations

from typing import List

from .base import Source
from .coingecko import CoinGeckoSource
from .defillama import DefiLlamaSource
from .github import GitHubSource
from .manual import ManualSource
from .rss import RssSource
from ..settings import Settings

__all__ = [
    "Source",
    "DefiLlamaSource",
    "CoinGeckoSource",
    "RssSource",
    "GitHubSource",
    "ManualSource",
    "build_sources",
]


def build_sources(settings: Settings) -> List[Source]:
    """Instantiate the sources enabled in the environment (order = priority)."""
    registry = {
        "defillama": lambda: DefiLlamaSource(),
        "coingecko": lambda: CoinGeckoSource(),
        "rss": lambda: RssSource(settings.rss_feeds, settings.watch_keywords),
        "github": lambda: GitHubSource(settings.github_queries, settings.bot_token and "" or ""),
        "manual": lambda: ManualSource(settings.projects_file),
    }
    sources: List[Source] = []
    for name in settings.enabled_sources:
        factory = registry.get(name.lower())
        if factory:
            sources.append(factory())
    if not sources:
        sources.append(ManualSource(settings.projects_file))
    return sources
