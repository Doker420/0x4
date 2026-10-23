"""Discovery orchestration: fetch → score → dedupe → persist."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import Airdrop
from .settings import settings
from .sources import ManualSource, build_sources
from .sources.base import new_client

log = logging.getLogger("airdrop.discovery")

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "offline.json"

STRONG_HINTS = (
    "airdrop",
    "air drop",
    "retroactive",
    "retro drop",
    "claim is live",
    "claim now",
    "snapshot",
    "token generation",
    "tge",
    "points season",
    "eligibility checker",
    "token claim",
)

SCAM_HINTS = (
    "giveaway",
    "double your",
    "send 0.1 eth",
    "seed phrase",
    "connect wallet to receive",
    "free crypto now",
)


def normalize_key(name: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "", (name or "").lower())
    return key[:60]


def recency_bonus(discovered_at: str) -> float:
    try:
        moment = datetime.fromisoformat((discovered_at or "").replace("Z", "+00:00"))
    except ValueError:
        return 0.5
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - moment).total_seconds() / 86400
    if age_days <= 3:
        return 3.0
    if age_days <= 14:
        return 1.5
    if age_days <= 60:
        return 0.5
    return 0.0


def score_item(item: Airdrop, source_weight: float) -> float:
    text = f"{item.name} {item.summary} {item.category}".lower()
    score = float(source_weight)
    hits = sum(1 for hint in STRONG_HINTS if hint in text)
    score += min(hits, 4) * 1.5
    if any(hint in text for hint in SCAM_HINTS):
        score -= 6.0
    if item.category in {"no-token-protocol", "funded-project"}:
        score += 1.5
    if item.deadline:
        score += 1.0
    if item.chain:
        score += 0.5
    score += recency_bonus(item.discovered_at)
    return round(max(score, 0.0), 2)


@dataclass
class DiscoveryReport:
    items: List[Airdrop] = field(default_factory=list)
    new_slugs: List[str] = field(default_factory=list)
    source_errors: Dict[str, str] = field(default_factory=dict)
    offline: bool = False

    @property
    def total(self) -> int:
        return len(self.items)

    def top(self, limit: int = 10) -> List[Airdrop]:
        return sorted(self.items, key=lambda item: item.score, reverse=True)[:limit]


def _load_fixtures() -> List[Dict[str, Any]]:
    if not FIXTURES.exists():
        return []
    try:
        data = json.loads(FIXTURES.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:  # pragma: no cover
        log.warning("cannot read fixtures: %s", exc)
        return []
    return [row for row in data if isinstance(row, dict)]


async def run_discovery(offline: bool = False, limit_per_source: int = 40) -> DiscoveryReport:
    """Fetch every enabled source, score the results and store them.

    `offline=True` never touches the network: it reads local fixtures + the
    operator's manual list. Used for tests and for the first dry run on a server
    without egress.
    """
    from . import db

    report = DiscoveryReport(offline=offline)
    weights: Dict[str, float] = {}
    raw: List[Airdrop] = []

    if offline:
        for row in _load_fixtures():
            raw.append(
                Airdrop(
                    name=row.get("name", ""),
                    url=row.get("url", ""),
                    source=row.get("source", "fixture"),
                    chain=row.get("chain", ""),
                    category=row.get("category", ""),
                    summary=row.get("summary", ""),
                    deadline=row.get("deadline"),
                    extra=row.get("extra", {}),
                )
            )
            weights.setdefault(row.get("source", "fixture"), 2.0)
        for item in await ManualSource(settings.projects_file).fetch():
            raw.append(item)
            weights.setdefault(ManualSource.name, ManualSource.weight)
    else:
        sources = build_sources(settings)
        async with new_client() as client:
            for source in sources:
                weights[source.name] = getattr(source, "weight", 1.0)
                outcome = await source.safe_fetch(client)
                if outcome.error:
                    report.source_errors[source.name] = outcome.error
                raw.extend(outcome.items[:limit_per_source])

    # score + dedupe -----------------------------------------------------
    best: Dict[str, Airdrop] = {}
    for item in raw:
        if not item.name:
            continue
        item.score = score_item(item, weights.get(item.source, 1.0))
        key = normalize_key(item.name)
        if not key:
            continue
        previous = best.get(key)
        if previous is None or item.score > previous.score:
            if previous is not None:
                item.summary = item.summary or previous.summary
                merged_sources = set(str(previous.source).split("+")) | {item.source}
                item.source = "+".join(sorted(merged_sources))
            best[key] = item

    report.items = list(best.values())

    for item in report.items:
        try:
            is_new = await db.upsert_project(item)
        except Exception as exc:  # noqa: BLE001
            log.warning("cannot store %s: %s", item.name, exc)
            continue
        if is_new:
            report.new_slugs.append(item.slug)

    await db.log_run("discovery", True, f"{len(report.items)} items, {len(report.new_slugs)} new")
    return report


async def offline_report(limit: int = 10) -> List[Airdrop]:
    report = await run_discovery(offline=True)
    return report.top(limit)
