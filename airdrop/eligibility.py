"""Eligibility checks.

Only public, read-only endpoints are queried, and only for projects the operator
listed in `config/airdrops.json` (a `check_url` template or an on-chain token
balance gate). Nothing here ever signs a transaction.

Supported check kinds
---------------------
* ``json``    — GET ``check_url`` with ``{address}`` substituted; the JSON reply is
                searched for amount / allocation / eligible keys.
* ``balance`` — on-chain ERC-20 or native balance must be above ``min_amount``.
* ``manual``  — no automated check; the operator gets the official claim URL.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import httpx

from . import chains, db
from .models import EligibilityResult, iso
from .settings import settings
from .sources.base import new_client

log = logging.getLogger("airdrop.eligibility")

_AMOUNT_KEYS = (
    "amount",
    "allocation",
    "tokenAmount",
    "value",
    "tokens",
    "reward",
    "claimable",
    "airdrop",
)
_FLAG_KEYS = ("eligible", "isEligible", "qualified", "claimable", "whitelisted")


class EligibilityError(RuntimeError):
    pass


def load_projects() -> List[Dict[str, Any]]:
    path: Path = settings.projects_file
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("cannot read %s: %s", path, exc)
        return []
    rows = data.get("airdrops") if isinstance(data, dict) else data
    return [row for row in (rows or []) if isinstance(row, dict)]


def _dig(node: Any, address: str) -> Optional[Dict[str, Any]]:
    """Walk a JSON reply looking for a mapping that describes the allocation."""
    if isinstance(node, dict):
        lowered = {str(key).lower(): key for key in node.keys()}
        if address.lower() in {str(value).lower() for value in node.values() if isinstance(value, str)}:
            return node
        # nested per-address mapping: {"0xabc...": {...}}
        for key, value in node.items():
            if isinstance(key, str) and key.lower() == address.lower() and isinstance(value, (dict, list, str, int, float)):
                return value if isinstance(value, dict) else {"amount": value}
        for key in _AMOUNT_KEYS:
            if key.lower() in lowered:
                return node
        for key in _FLAG_KEYS:
            if key.lower() in lowered:
                return node
        for value in node.values():
            found = _dig(value, address)
            if found:
                return found
    elif isinstance(node, list):
        for value in node[:200]:
            found = _dig(value, address)
            if found:
                return found
    return None


async def check_json(project: Dict[str, Any], address: str, client: httpx.AsyncClient) -> EligibilityResult:
    template = project.get("check_url") or ""
    url = template.replace("{address}", address)
    if not url.startswith("http"):
        raise EligibilityError("check_url не задан или некорректен")
    response = await client.get(
        url, headers={"User-Agent": settings.user_agent, "Accept": "application/json"}
    )
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as exc:
        raise EligibilityError("Ответ не JSON — возможно, это не API, а веб-страница.") from exc

    node = _dig(payload, address)
    if node is None:
        return EligibilityResult(
            project=project.get("name", ""),
            address=address,
            eligible=False,
            source=url,
            claim_url=project.get("claim_url", ""),
        )
    amount = ""
    for key in _AMOUNT_KEYS:
        for real_key, value in node.items():
            if str(real_key).lower() == key.lower() and isinstance(value, (int, float, str)):
                amount = str(value)
                break
        if amount:
            break
    eligible = bool(amount and str(amount) not in {"0", "0.0", ""})
    if not eligible:
        for key in _FLAG_KEYS:
            for real_key, value in node.items():
                if str(real_key).lower() == key.lower():
                    eligible = bool(value)
                    if eligible:
                        amount = amount or "yes"
    return EligibilityResult(
        project=project.get("name", ""),
        address=address,
        eligible=eligible,
        amount=amount,
        token=project.get("token", ""),
        claim_url=project.get("claim_url", ""),
        source=url,
    )


def check_balance(project: Dict[str, Any], address: str) -> EligibilityResult:
    chain = project.get("chain") or settings.default_chain
    token = project.get("token_address") or ""
    minimum = float(project.get("min_amount", 0) or 0)
    try:
        if token:
            result = chains.token_balance(chain, token, address)
            value = float(result["value"])
            symbol = result["symbol"] or project.get("token", "")
        else:
            value = chains.native_balance(chain, address)
            symbol = "ETH"
    except Exception as exc:  # noqa: BLE001
        return EligibilityResult(
            project=project.get("name", ""),
            address=address,
            eligible=False,
            error=str(exc)[:300],
            source=f"rpc:{chain}",
        )
    return EligibilityResult(
        project=project.get("name", ""),
        address=address,
        eligible=value >= minimum,
        amount=f"{value:,.6f}",
        token=symbol,
        claim_url=project.get("claim_url", ""),
        source=f"rpc:{chain}",
    )


async def check_project(project: Dict[str, Any], address: str, client: Optional[httpx.AsyncClient] = None) -> EligibilityResult:
    kind = (project.get("check_kind") or ("json" if project.get("check_url") else "manual")).lower()
    try:
        if kind == "json":
            owns_client = client is None
            client = client or new_client()
            try:
                result = await check_json(project, address, client)
            finally:
                if owns_client:
                    await client.aclose()
        elif kind == "balance":
            result = check_balance(project, address)
        else:
            result = EligibilityResult(
                project=project.get("name", ""),
                address=address,
                eligible=False,
                claim_url=project.get("claim_url", ""),
                source="manual",
                error="Автопроверка не настроена — откройте официальную страницу.",
            )
    except Exception as exc:  # noqa: BLE001
        result = EligibilityResult(
            project=project.get("name", ""),
            address=address,
            eligible=False,
            error=str(exc)[:300],
            source=kind,
        )
    result.checked_at = iso()
    await db.save_eligibility(result)
    return result


async def check_all(addresses: Iterable[str], projects: Optional[List[Dict[str, Any]]] = None) -> List[EligibilityResult]:
    rows = projects if projects is not None else load_projects()
    rows = [row for row in rows if row.get("check_url") or row.get("check_kind") in {"balance", "manual"}]
    results: List[EligibilityResult] = []
    async with new_client() as client:
        for address in addresses:
            for project in rows:
                results.append(await check_project(project, address, client))
    await db.log_run("eligibility", True, f"{len(results)} checks")
    return results
