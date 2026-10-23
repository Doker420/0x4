"""Operator-maintained project list (config/airdrops.json).

This is the only source that may describe HOW to claim something, because the
operator verified the contract/URL by hand. Nothing here is fetched from
third-party sites.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..models import Airdrop
from .base import Source, clean_text

log = logging.getLogger("airdrop.sources.manual")


class ManualSource(Source):
    name = "manual"
    weight = 6.0

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path

    def _rows(self) -> List[Dict[str, Any]]:
        if not self.path:
            return []
        try:
            data = json.loads(Path(self.path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            log.warning("cannot read %s: %s", self.path, exc)
            return []
        if isinstance(data, dict):
            rows = data.get("airdrops") or data.get("projects") or []
        else:
            rows = data
        return [row for row in rows if isinstance(row, dict)]

    async def fetch(self, client: object = None) -> List[Airdrop]:  # noqa: ARG002
        out: List[Airdrop] = []
        for row in self._rows():
            name = clean_text(row.get("name") or row.get("project") or "", 80)
            url = clean_text(row.get("url") or row.get("claim_url") or "", 300)
            claim_url = clean_text(row.get("claim_url") or "", 300)
            # Never surface documentation placeholders as live opportunities, even if
            # an old deployment copied the example config into a real config file.
            combined_urls = f"{url} {claim_url}".lower()
            if any(marker in combined_urls for marker in ("example.com", "example.org", "example.net", "example.test")):
                log.warning("skipping placeholder project URL in %s: %s", self.path, name)
                continue
            if not name:
                continue
            out.append(
                Airdrop(
                    name=name,
                    url=clean_text(row.get("url") or row.get("claim_url") or "", 300),
                    source=self.name,
                    chain=clean_text(row.get("chain") or "", 24),
                    category=clean_text(row.get("category") or "manual", 40),
                    summary=clean_text(row.get("summary") or row.get("notes") or "", 400),
                    deadline=row.get("deadline"),
                    extra={key: value for key, value in row.items() if key.startswith(("claim_", "check_", "token", "amount"))},
                )
            )
        return out
