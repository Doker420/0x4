"""Eligibility checks against mocked HTTP/RPC endpoints."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from airdrop import chains, db, eligibility
from airdrop.settings import settings


class EligibilityTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        settings.data_dir = Path(self.tmp.name)
        settings.keystore_dir = Path(self.tmp.name) / "keystore"
        settings.ensure_dirs()
        await db.init_db()

    def _client(self, payload):
        def handler(request: httpx.Request) -> httpx.Response:
            self.last_url = str(request.url)
            return httpx.Response(200, json=payload)

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def test_json_amount_found(self) -> None:
        payload = {"allocation": "1250.5"}
        project = {"name": "Drop", "check_kind": "json", "check_url": "https://api.test/alloc/{address}", "token": "DRP"}
        async with self._client(payload) as client:
            result = await eligibility.check_project(project, "0xAbC0000000000000000000000000000000000001", client)
        self.assertTrue(result.eligible)
        self.assertEqual(result.amount, "1250.5")
        self.assertEqual(result.token, "DRP")
        self.assertIn("0xAbC0000000000000000000000000000000000001", self.last_url)

    async def test_nested_per_address_mapping(self) -> None:
        payload = {"claims": {"0xabc0000000000000000000000000000000000001": {"amount": "10"}}}
        project = {"name": "Drop", "check_kind": "json", "check_url": "https://api.test/{address}"}
        async with self._client(payload) as client:
            result = await eligibility.check_project(project, "0xAbC0000000000000000000000000000000000001", client)
        self.assertTrue(result.eligible)
        self.assertEqual(result.amount, "10")

    async def test_zero_allocation_is_not_eligible(self) -> None:
        project = {"name": "Drop", "check_kind": "json", "check_url": "https://api.test/{address}"}
        async with self._client({"amount": "0"}) as client:
            result = await eligibility.check_project(project, "0xabc0000000000000000000000000000000000001", client)
        self.assertFalse(result.eligible)

    async def test_eligible_flag_without_amount(self) -> None:
        project = {"name": "Drop", "check_kind": "json", "check_url": "https://api.test/{address}"}
        async with self._client({"eligible": True}) as client:
            result = await eligibility.check_project(project, "0xabc0000000000000000000000000000000000001", client)
        self.assertTrue(result.eligible)

    async def test_non_json_response_is_an_error_not_a_crash(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>not json</html>")

        project = {"name": "Drop", "check_kind": "json", "check_url": "https://api.test/{address}"}
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await eligibility.check_project(project, "0xabc0000000000000000000000000000000000001", client)
        self.assertFalse(result.eligible)
        self.assertIn("не JSON", result.error)

    async def test_http_error_is_captured(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"error": "nope"})

        project = {"name": "Drop", "check_kind": "json", "check_url": "https://api.test/{address}"}
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await eligibility.check_project(project, "0xabc0000000000000000000000000000000000001", client)
        self.assertFalse(result.eligible)
        self.assertTrue(result.error)

    async def test_missing_check_url_is_manual(self) -> None:
        result = await eligibility.check_project({"name": "Manual"}, "0xabc0000000000000000000000000000000000001")
        self.assertFalse(result.eligible)
        self.assertEqual(result.source, "manual")

    async def test_balance_check_uses_rpc(self) -> None:
        project = {
            "name": "BalanceGate",
            "check_kind": "balance",
            "chain": "base",
            "token_address": "0x" + "22" * 20,
            "min_amount": 5,
        }
        with mock.patch.object(chains, "token_balance", return_value={"raw": 10**18, "value": 10.0, "symbol": "TKN", "decimals": 18}):
            result = eligibility.check_balance(project, "0xabc0000000000000000000000000000000000001")
        self.assertTrue(result.eligible)
        self.assertEqual(result.token, "TKN")

    async def test_results_are_persisted(self) -> None:
        project = {"name": "Drop", "check_kind": "json", "check_url": "https://api.test/{address}"}
        async with self._client({"amount": "3"}) as client:
            await eligibility.check_project(project, "0xabc0000000000000000000000000000000000001", client)
        rows = await db.list_eligibility()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["amount"], "3")
        # second check updates instead of duplicating
        async with self._client({"amount": "4"}) as client:
            await eligibility.check_project(project, "0xabc0000000000000000000000000000000000001", client)
        rows = await db.list_eligibility()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["amount"], "4")


if __name__ == "__main__":
    unittest.main()
