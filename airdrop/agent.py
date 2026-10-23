"""Main agent loop: discover → notify → re-check eligibility.

Usage
-----
    python -m airdrop.agent --once            # one discovery pass
    python -m airdrop.agent --once --offline  # fixtures only, no network
    python -m airdrop.agent --eligibility     # run eligibility checks for keystore wallets
    python -m airdrop.agent                   # daemon loop
    python -m airdrop.agent --status          # print stored stats
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
from typing import Any, Dict, List

from . import db, eligibility, wallet
from .discovery import run_discovery
from .notify import notifier
from .settings import load_settings, settings

log = logging.getLogger("airdrop.agent")

_STOP = False


def setup_logging(level: int = logging.INFO) -> None:
    settings.ensure_dirs()
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(settings.log_path, encoding="utf-8"),
        ],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("web3").setLevel(logging.WARNING)


async def discovery_pass(offline: bool = False) -> Dict[str, Any]:
    report = await run_discovery(offline=offline)
    fresh = [item for item in report.items if item.slug in report.new_slugs]
    await notifier.new_drops(fresh)
    if fresh:
        await db.mark_notified([item.slug for item in fresh])
    log.info("discovery: %s items, %s new, errors=%s", report.total, len(report.new_slugs), report.source_errors)
    return {
        "total": report.total,
        "new": len(report.new_slugs),
        "errors": report.source_errors,
        "offline": report.offline,
    }


async def eligibility_pass() -> Dict[str, Any]:
    """Check every keystore wallet against every project with a configured check."""
    projects = eligibility.load_projects()
    if not projects:
        log.info("eligibility: нет проектов с проверкой (config/airdrops.json)")
        await db.log_run("eligibility", True, "no projects configured")
        return {"checked": 0, "results": []}
    try:
        names = [entry["name"] for entry in wallet.list_wallets()]
        addresses = [wallet.wallet_address(name) for name in names]
    except wallet.WalletError as exc:
        log.warning("eligibility: %s", exc)
        await db.log_run("eligibility", False, str(exc))
        return {"checked": 0, "error": str(exc)}

    results = await eligibility.check_all(addresses, projects)
    positive = [row for row in results if row.eligible]
    await notifier.eligibility(positive)
    log.info("eligibility: %s checks, %s eligible", len(results), len(positive))
    return {"checked": len(results), "eligible": len(positive), "results": [row.as_dict() for row in positive]}


async def loop(offline: bool = False) -> None:
    global _STOP
    setup_logging()
    log.info("agent started (interval=%s min, dry_run=%s)", settings.discovery_interval_min, settings.dry_run)

    def _stop(*_args: Any) -> None:
        global _STOP
        _STOP = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _stop)  # type: ignore[arg-type]
        except (ValueError, OSError):  # pragma: no cover - non-main thread
            pass

    minutes_since_eligibility = 0
    while not _STOP:
        if await db.get_kv("paused", "0") == "1":
            log.debug("agent paused from the bot")
        else:
            try:
                await discovery_pass(offline=offline)
            except Exception as exc:  # noqa: BLE001
                log.exception("discovery pass failed: %s", exc)
                await db.log_run("discovery", False, str(exc)[:500])

        minutes_since_eligibility += settings.discovery_interval_min
        if minutes_since_eligibility >= settings.eligibility_interval_min and await db.get_kv("paused", "0") != "1":
            try:
                await eligibility_pass()
            except Exception as exc:  # noqa: BLE001
                log.exception("eligibility pass failed: %s", exc)
            minutes_since_eligibility = 0

        try:
            await asyncio.wait_for(_sleep_minutes(settings.discovery_interval_min), timeout=None)
        except asyncio.CancelledError:  # pragma: no cover
            break
    await notifier.close()
    log.info("agent stopped")


async def _sleep_minutes(minutes: int) -> None:
    seconds = max(int(minutes), 1) * 60
    slept = 0
    while slept < seconds and not _STOP:
        await asyncio.sleep(min(5, seconds - slept))
        slept += 5


async def status() -> Dict[str, Any]:
    stats = await db.stats()
    last_discovery = await db.last_run("discovery")
    last_eligibility = await db.last_run("eligibility")
    wallets = [entry["name"] for entry in wallet.list_wallets()]
    return {
        "stats": stats,
        "last_discovery": last_discovery,
        "last_eligibility": last_eligibility,
        "wallets": wallets,
        "dry_run": settings.dry_run,
        "sources": settings.enabled_sources,
        "interval_min": settings.discovery_interval_min,
    }


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="0x4 airdrop agent")
    parser.add_argument("--once", action="store_true", help="один проход discovery и выход")
    parser.add_argument("--offline", action="store_true", help="без сети: локальные фикстуры + config/airdrops.json")
    parser.add_argument("--eligibility", action="store_true", help="проверить кошельки на eligibility")
    parser.add_argument("--status", action="store_true", help="показать статистику и выйти")
    parser.add_argument("--loop", action="store_true", help="демон (по умолчанию без --once)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    setup_logging(logging.DEBUG if args.verbose else logging.INFO)
    settings = load_settings()
    settings.ensure_dirs()

    if args.status:
        print(json.dumps(asyncio.run(status()), ensure_ascii=False, indent=2, default=str))
        return 0

    async def runner() -> None:
        await db.init_db()
        if args.eligibility:
            print(json.dumps(await eligibility_pass(), ensure_ascii=False, indent=2, default=str))
        if args.once:
            print(json.dumps(await discovery_pass(offline=args.offline), ensure_ascii=False, indent=2, default=str))
        if args.loop and not args.once:
            await loop(offline=args.offline)
        if not (args.once or args.eligibility or args.loop):
            print(json.dumps(await discovery_pass(offline=args.offline), ensure_ascii=False, indent=2, default=str))
        await notifier.close()

    asyncio.run(runner())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
