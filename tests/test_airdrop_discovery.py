"""Discovery: scoring, dedupe, scam penalty, persistence."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from airdrop import db
from airdrop.discovery import normalize_key, recency_bonus, run_discovery, score_item
from airdrop.models import Airdrop
from airdrop.settings import settings


class ScoringTestCase(unittest.TestCase):
    def test_keyword_bonus(self) -> None:
        rich = Airdrop(name="X", summary="airdrop claim is live, snapshot taken", category="airdrop-listing")
        poor = Airdrop(name="X", summary="just another protocol update", category="other")
        self.assertGreater(score_item(rich, 1.0), score_item(poor, 1.0) + 2)

    def test_scam_penalty(self) -> None:
        scam = Airdrop(name="Free crypto", summary="Giveaway! Send 0.1 ETH, double your funds")
        normal = Airdrop(name="Normal project", summary="DeFi protocol with points season")
        self.assertLess(score_item(scam, 2.0), score_item(normal, 2.0))

    def test_score_never_negative(self) -> None:
        item = Airdrop(name="scam", summary="giveaway double your send 0.1 eth seed phrase connect wallet to receive")
        self.assertGreaterEqual(score_item(item, 0.0), 0.0)

    def test_recency_bonus_decays(self) -> None:
        self.assertGreater(recency_bonus("2026-10-22T00:00:00+00:00"), recency_bonus("2020-01-01T00:00:00+00:00"))

    def test_normalize_key(self) -> None:
        self.assertEqual(normalize_key("Base  Protocol!"), normalize_key("base protocol"))


class DiscoveryRunTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        settings.data_dir = Path(self.tmp.name)
        settings.keystore_dir = Path(self.tmp.name) / "keystore"
        settings.projects_file = Path(self.tmp.name) / "missing.json"
        settings.ensure_dirs()
        await db.init_db()

    async def test_offline_run_stores_projects(self) -> None:
        report = await run_discovery(offline=True)
        self.assertTrue(report.offline)
        self.assertGreaterEqual(report.total, 3)
        self.assertEqual(len(report.new_slugs), report.total)

        stored = await db.list_projects(limit=50)
        self.assertEqual(len(stored), report.total)

    async def test_second_run_reports_nothing_new(self) -> None:
        await run_discovery(offline=True)
        report = await run_discovery(offline=True)
        self.assertEqual(report.new_slugs, [])

    async def test_ranking_and_scam_filtering(self) -> None:
        report = await run_discovery(offline=True)
        top = report.top(5)
        scores = [item.score for item in top]
        self.assertEqual(scores, sorted(scores, reverse=True))
        scam = [item for item in report.items if "example-scam" in item.url]
        self.assertTrue(scam)
        self.assertLess(scam[0].score, settings.notify_score_threshold)

    async def test_manual_source_is_highest_priority(self) -> None:
        manual = Path(self.tmp.name) / "airdrops.json"
        manual.write_text(
            '{"airdrops": [{"name": "MyVerifiedDrop", "chain": "base", "url": "https://x.test", '
            '"claim_url": "https://x.test/claim", "summary": "verified airdrop claim", "deadline": "2026-12-01"}]}',
            encoding="utf-8",
        )
        settings.projects_file = manual
        report = await run_discovery(offline=True)
        names = {item.name for item in report.items}
        self.assertIn("MyVerifiedDrop", names)
        top = report.top(1)[0]
        self.assertEqual(top.name, "MyVerifiedDrop")


if __name__ == "__main__":
    unittest.main()
