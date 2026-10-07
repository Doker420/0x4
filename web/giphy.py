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


async def search_gif(query: str, limit: int = 20) -> List[str]:
    query = (query or "").strip()[:120]
    if not query:
        return []
    limit = max(1, min(int(limit), 50))
    giphy_key, tenor_key = await _get_keys()
    urls: List[str] = []

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        if giphy_key:
            try:
                response = await client.get(
                    "https://api.giphy.com/v1/gifs/search",
                    params={"api_key": giphy_key, "q": query, "limit": limit, "rating": "pg-13"},
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
                    params={"key": tenor_key, "q": query, "limit": limit, "media_filter": "gif"},
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


async def random_gif(query: str = "") -> Optional[str]:
    urls = await search_gif(query or "funny", limit=25)
    return random.choice(urls) if urls else None


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
