from __future__ import annotations

import logging
import os
import random
import uuid
from pathlib import Path
from typing import Any, List, Optional
from urllib.parse import urlparse

import httpx

from . import db

log = logging.getLogger("web.giphy")
GIPHY_KEY = os.getenv("GIPHY_KEY", "").strip()
TENOR_KEY = os.getenv("TENOR_KEY", "").strip()
MAX_GIF_BYTES = 20 * 1024 * 1024


async def _get_keys() -> tuple[str, str]:
    giphy_key = GIPHY_KEY or (await db.get_setting("giphy_key", "")).strip()
    tenor_key = TENOR_KEY or (await db.get_setting("tenor_key", "")).strip()
    return giphy_key, tenor_key


# Neutral tags used when a turn has no text to search by, so every account that
# posts "just a gif" gets a different kind of reaction instead of the same one.
RANDOM_GIF_TAGS = (
    "funny", "lol", "reaction", "excited", "okay", "thumbs up", "no way",
    "surprised", "happy dance", "eye roll", "thinking", "celebrate",
)


async def search_gif(query: str, limit: int = 20, offset: int = 0) -> List[str]:
    query = (query or "").strip()[:120]
    if not query:
        return []
    limit = max(1, min(int(limit), 50))
    offset = max(0, int(offset))
    giphy_key, tenor_key = await _get_keys()
    urls: List[str] = []

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        if giphy_key:
            try:
                response = await client.get(
                    "https://api.giphy.com/v1/gifs/search",
                    params={
                        "api_key": giphy_key, "q": query, "limit": limit,
                        "offset": offset, "rating": "pg-13",
                    },
                )
                response.raise_for_status()
                for item in response.json().get("data", []):
                    images = item.get("images", {})
                    url = (
                        images.get("downsized", {}).get("url")
                        or images.get("fixed_height", {}).get("url")
                        or images.get("original", {}).get("url")
                    )
                    if url:
                        urls.append(str(url))
            except Exception as exc:
                log.warning("GIPHY search failed (%s)", type(exc).__name__)

        if not urls and tenor_key:
            try:
                response = await client.get(
                    "https://tenor.googleapis.com/v2/search",
                    params={
                        "key": tenor_key, "q": query, "limit": limit,
                        "pos": offset, "media_filter": "gif",
                    },
                )
                response.raise_for_status()
                for item in response.json().get("results", []):
                    formats = item.get("media_formats", {})
                    media = formats.get("gif") or formats.get("mediumgif") or formats.get("tinygif")
                    url = media.get("url") if isinstance(media, dict) else None
                    if url:
                        urls.append(str(url))
            except Exception as exc:
                log.warning("Tenor search failed (%s)", type(exc).__name__)

    return urls


async def _giphy_random_url(client: httpx.AsyncClient, key: str, tag: str) -> Optional[str]:
    """GIPHY has a dedicated random endpoint: a different gif on every call."""
    try:
        response = await client.get(
            "https://api.giphy.com/v1/gifs/random",
            params={"api_key": key, "tag": tag, "rating": "pg-13"},
        )
        response.raise_for_status()
        images = ((response.json() or {}).get("data") or {}).get("images") or {}
        url = (
            images.get("downsized", {}).get("url")
            or images.get("fixed_height", {}).get("url")
            or images.get("original", {}).get("url")
        )
        return str(url) if url else None
    except Exception as exc:
        log.warning("GIPHY random failed (%s)", type(exc).__name__)
        return None


async def random_gif(query: str = "", avoid: Optional[List[str]] = None) -> Optional[str]:
    """Return a random gif, trying hard not to repeat one that was just used.

    Uses the provider's random endpoint plus a randomised search offset, so two
    accounts reacting to the same line still get different gifs.
    """
    tag = (query or "").strip()[:120] or random.choice(RANDOM_GIF_TAGS)
    skipped = {str(url) for url in (avoid or ()) if url}
    giphy_key, tenor_key = await _get_keys()

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        candidates: List[str] = []
        if giphy_key:
            url = await _giphy_random_url(client, giphy_key, tag)
            if url:
                candidates.append(url)
            if tag in RANDOM_GIF_TAGS:
                # A neutral tag means "anything funny works": widen the pool.
                url = await _giphy_random_url(client, giphy_key, random.choice(RANDOM_GIF_TAGS))
                if url:
                    candidates.append(url)
        if tenor_key:
            offset = random.randint(0, 60)
            try:
                response = await client.get(
                    "https://tenor.googleapis.com/v2/search",
                    params={
                        "key": tenor_key, "q": tag, "limit": 10,
                        "pos": offset, "media_filter": "gif",
                    },
                )
                response.raise_for_status()
                for item in response.json().get("results", []):
                    formats = item.get("media_formats", {})
                    media = formats.get("gif") or formats.get("mediumgif") or formats.get("tinygif")
                    url = media.get("url") if isinstance(media, dict) else None
                    if url:
                        candidates.append(str(url))
            except Exception as exc:
                log.warning("Tenor random search failed (%s)", type(exc).__name__)
        if not candidates:
            candidates = await search_gif(tag, limit=25, offset=random.randint(0, 60))

        fresh = [url for url in dict.fromkeys(candidates) if url not in skipped]
        return random.choice(fresh or list(dict.fromkeys(candidates))) if (fresh or candidates) else None


async def download_gif(url: str, directory: Path) -> Path:
    """Download a provider GIF to a small local file for reliable Telegram upload."""
    directory.mkdir(parents=True, exist_ok=True)
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix not in {".gif", ".mp4", ".webm"}:
        suffix = ".gif"
    destination = directory / f"{uuid.uuid4().hex}{suffix}"
    written = 0
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").lower()
                if "video/mp4" in content_type:
                    destination = destination.with_suffix(".mp4")
                elif "video/webm" in content_type:
                    destination = destination.with_suffix(".webm")
                elif "image/gif" in content_type:
                    destination = destination.with_suffix(".gif")
                with destination.open("wb") as output:
                    async for chunk in response.aiter_bytes(64 * 1024):
                        written += len(chunk)
                        if written > MAX_GIF_BYTES:
                            raise ValueError("GIF больше лимита 20 МБ")
                        output.write(chunk)
        if written == 0:
            raise ValueError("Провайдер вернул пустой GIF")
        return destination
    except Exception:
        destination.unlink(missing_ok=True)
        raise


async def search_meme_photo(query: str = "funny meme", limit: int = 20) -> List[str]:
    """Fetch one safe-for-work meme image from meme-api.com."""
    del query, limit
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            response = await client.get("https://meme-api.com/gimme/wholesomememes")
            response.raise_for_status()
            data = response.json()
            return [str(data["url"])] if data.get("url") else []
    except Exception as exc:
        log.warning("Meme image lookup failed (%s)", type(exc).__name__)
        return []


async def random_photo() -> Optional[str]:
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
            response = await client.get("https://picsum.photos/800/600")
            if response.status_code in (301, 302, 303, 307, 308):
                return response.headers.get("location")
    except Exception as exc:
        log.warning("Random photo lookup failed (%s)", type(exc).__name__)
    return None
