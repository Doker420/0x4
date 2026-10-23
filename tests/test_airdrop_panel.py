"""Tests for the :1111 web panel (monitoring, Exodus wallet export, forecast, guide)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from airdrop import db, panel, wallet
from airdrop.settings import settings

PASSPHRASE = "correct-horse-battery-staple"


class PanelTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        settings.data_dir = Path(self.tmp.name)
        settings.keystore_dir = Path(self.tmp.name) / "keystore"
        settings.db_path = Path(self.tmp.name) / "airdrop.sqlite3"
        settings.log_path = Path(self.tmp.name) / "agent.log"
        settings.projects_file = Path(self.tmp.name) / "airdrops.json"
        settings.ensure_dirs()
        os.environ["AIRDROP_KEY_PASSPHRASE"] = PASSPHRASE
        os.environ["AIRDROP_AUTOSTART_SIBLINGS"] = "0"
        await db.init_db()
        await panel._on_startup()

    async def _get(self, path: str) -> httpx.Response:
        transport = httpx.ASGITransport(app=panel.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get(path, follow_redirects=True)

    async def _post(self, path: str, data: dict) -> httpx.Response:
        transport = httpx.ASGITransport(app=panel.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post(path, data=data, follow_redirects=True)

    async def test_health_endpoint_lists_four_services_and_wallet(self) -> None:
        resp = await self._get("/api/health")
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["services"]), 4)
        self.assertGreaterEqual(payload["wallets_count"], 1)

    async def test_dashboard_renders_services_and_checklist(self) -> None:
        resp = await self._get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Мониторинг 4 сервисов", resp.text)
        self.assertIn("Подключить в Exodus", resp.text)

    async def test_wallets_reveal_shows_12_words_for_exodus(self) -> None:
        resp = await self._get("/wallets?reveal=main")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Secret Recovery Phrase", resp.text)
        secrets = wallet.export_wallet_secrets("main", PASSPHRASE)
        self.assertEqual(len(secrets["mnemonic"].split()), 12)
        self.assertIn(secrets["mnemonic"], resp.text)
        self.assertIn(secrets["private_key"], resp.text)

    async def test_create_and_import_mnemonic_via_panel(self) -> None:
        resp = await self._post("/wallets/create", {"name": "exodus2"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("exodus2", resp.text)

        sec = wallet.export_wallet_secrets("exodus2", PASSPHRASE)
        resp2 = await self._post(
            "/wallets/import",
            {"name": "imported_from_exodus", "secret_value": sec["mnemonic"]},
        )
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(
            wallet.wallet_address("imported_from_exodus", PASSPHRASE).lower(),
            sec["address"].lower(),
        )

    async def test_forecast_and_guide_pages(self) -> None:
        resp_f = await self._get("/forecast?hours=2&budget=50&services_income=1")
        self.assertEqual(resp_f.status_code, 200)
        self.assertIn("Первые 24–48 часов", resp_f.text)
        self.assertIn("Learn &amp; Earn", resp_f.text)

        resp_g = await self._get("/guide")
        self.assertEqual(resp_g.status_code, 200)
        self.assertIn("Exodus", resp_g.text)
        self.assertIn("Rabby", resp_g.text)
