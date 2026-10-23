"""Crypto media RSS: catches announcements the aggregator APIs miss."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import List

import httpx

from ..models import Airdrop, iso
from .base import Source, clean_text, get_text, guess_chain

log = logging.getLogger("airdrop.sources.rss")


def _parse_feed(text: str) -> List[dict]:
    try:
        import feedparser  # type: ignore
    except ImportError:  # pragma: no cover
        return _fallback_parse(text)
    parsed = feedparser.parse(text)
    entries = []
    for entry in (parsed.entries or [])[:40]:
        published = ""
        if getattr(entry, "published_parsed", None):
            try:
                published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc).isoformat(timespec="seconds")
            except Exception:  # noqa: BLE001
                published = ""
        entries.append(
            {
                "title": clean_text(getattr(entry, "title", ""), 160),
                "link": getattr(entry, "link", ""),
                "summary": clean_text(getattr(entry, "summary", ""), 300),
                "published": published,
            }
        )
    return entries


def _fallback_parse(text: str) -> List[dict]:
    entries = []
    for match in re.finditer(r"<item>(.*?)</item>", text or "", re.S):
        chunk = match.group(1)

        def grab(tag: str) -> str:
            found = re.search(rf"<{tag}>(.*?)</{tag}>", chunk, re.S)
            return clean_text(re.sub(r"<[^>]+>", "", found.group(1)) if found else "", 300)

        entries.append({"title": grab("title")[:160], "link": grab("link"), "summary": grab("description"), "published": grab("pubDate")})
    return entries


class RssSource(Source):
    name = "rss"
    weight = 2.5

    def __init__(self, feeds: List[str], keywords: List[str]) -> None:
        self.feeds = [feed for feed in feeds if feed]
        self.keywords = [word.lower() for word in keywords if word]

    def relevant(self, text: str) -> bool:
        lowered = (text or "").lower()
        return any(word in lowered for word in self.keywords)

    async def fetch(self, client: httpx.AsyncClient) -> List[Airdrop]:
        out: List[Airdrop] = []
        for feed in self.feeds:
            try:
                text = await get_text(client, feed)
            except Exception as exc:  # noqa: BLE001
                log.debug("rss %s unavailable: %s", feed, exc)
                continue
            entries = await asyncio.to_thread(_parse_feed, text)
            for entry in entries:
                haystack = f"{entry.get('title', '')} {entry.get('summary', '')}"
                if not self.relevant(haystack):
                    continue
                out.append(
                    Airdrop(
                        name=clean_text(entry.get("title", ""), 120) or feed,
                        url=entry.get("link", ""),
                        source=self.name,
                        chain=guess_chain(haystack),
                        category="news",
                        summary=clean_text(entry.get("summary", "")),
                        discovered_at=entry.get("published") or iso(),
                        extra={"feed": feed},
                    )
                )
        return out
