"""GitHub search: community-maintained airdrop trackers (public API, read-only).

Used only to surface curated public lists so the operator can review them by hand.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

import httpx

from ..models import Airdrop
from .base import Source, clean_text, dict_get, get_json, guess_chain

log = logging.getLogger("airdrop.sources.github")

SEARCH_URL = "https://api.github.com/search/repositories"


class GitHubSource(Source):
    name = "github"
    weight = 1.0

    def __init__(self, queries: List[str], token: str = "") -> None:
        self.queries = [query for query in queries if query]
        self.token = token

    async def fetch(self, client: httpx.AsyncClient) -> List[Airdrop]:
        headers: Dict[str, str] = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        out: List[Airdrop] = []
        for query in self.queries:
            # '+' in a config value means "space" for humans; httpx would escape
            # a literal '+' and GitHub would return nothing.
            params = {"q": query.replace("+", " ").strip(), "sort": "updated", "order": "desc", "per_page": 10}
            try:
                data = await get_json(client, SEARCH_URL, params=params, headers=headers or None)
            except Exception as exc:  # noqa: BLE001
                log.debug("github search unavailable for %s: %s", query, exc)
                continue
            for repo in (data or {}).get("items", [])[:10]:
                if not isinstance(repo, dict):
                    continue
                full_name = clean_text(dict_get(repo, "full_name", default=""), 100)
                if not full_name:
                    continue
                description = clean_text(dict_get(repo, "description", default=""), 250)
                out.append(
                    Airdrop(
                        name=full_name,
                        url=dict_get(repo, "html_url", default=""),
                        source=self.name,
                        chain=guess_chain(description),
                        category="tracker-repo",
                        summary=description,
                        discovered_at=clean_text(dict_get(repo, "updated_at", default=""), 40) or None
                        or "",
                        extra={
                            "stars": dict_get(repo, "stargazers_count", default=0),
                            "pushed_at": dict_get(repo, "pushed_at", default=""),
                        },
                    )
                )
        return out
