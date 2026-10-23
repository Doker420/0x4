"""Plain data containers shared by the airdrop agent modules."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, Optional


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: Optional[datetime] = None) -> str:
    return (moment or utcnow()).astimezone(timezone.utc).isoformat(timespec="seconds")


def slugify(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "-", (value or "").strip().lower())
    return value.strip("-")[:80] or "project"


@dataclass
class Airdrop:
    """A candidate airdrop discovered from a public source."""

    name: str
    url: str = ""
    source: str = "manual"
    chain: str = ""
    category: str = ""
    score: float = 0.0
    summary: str = ""
    deadline: Optional[str] = None
    discovered_at: str = field(default_factory=iso)
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def slug(self) -> str:
        return slugify(f"{self.source}-{self.name}")

    def to_row(self) -> Dict[str, Any]:
        data = asdict(self)
        data["slug"] = self.slug
        data["extra"] = self.extra
        return data


@dataclass
class EligibilityResult:
    project: str
    address: str
    eligible: bool
    amount: str = ""
    token: str = ""
    claim_url: str = ""
    source: str = ""
    error: str = ""
    checked_at: str = field(default_factory=iso)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ActionResult:
    kind: str
    ok: bool
    address: str = ""
    chain: str = ""
    project: str = ""
    tx_hash: str = ""
    message: str = ""
    value_eth: float = 0.0
    dry_run: bool = True
    created_at: str = field(default_factory=iso)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)
