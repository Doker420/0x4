"""Safety guards: nothing is ever sent unless every check passes."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from airdrop import actions, db, wallet
from airdrop.settings import settings

PASSPHRASE = "correct-horse-battery-staple"
ROUTER = "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"
CLAIM_CONTRACT = "0x1111111111111111111111111111111111111111"


class ActionTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        settings.data_dir = Path(self.tmp.name)
        settings.keystore_dir = Path(self.tmp.name) / "keystore"
        settings.ensure_dirs()
        settings.rpc = {"ethereum": "http://rpc.test"}
        settings.dry_run = True
        settings.max_tx_value_eth = 0.01
        settings.daily_limit_eth = 0.05
        settings.max_gas_gwei = 25.0
        settings.allow_unknown_contracts = False
        await db.init_db()
        wallet.create_wallet("main", PASSPHRASE)

    def _allow(self, entries):
        return mock.patch.object(actions, "load_allowlist", lambda: entries)

    def _chains_stub(self, gas=5.0, is_contract=True, w3=None):
        stub = mock.MagicMock()
        stub.gas_gwei.return_value = gas
        stub.is_contract.return_value = is_contract
        stub.client.return_value = w3 or mock.MagicMock()
        stub.explorer_tx_url.return_value = "https://explorer/tx/0xabc"
        return stub

    # ----------------------------------------------------------------- #
    async def test_unknown_contract_is_refused(self) -> None:
        with self._allow({"swap": []}):
            with self.assertRaises(actions.ActionError):
                await actions.precheck("ethereum", 0.001, ROUTER, "swap")

    async def test_allowlisted_contract_passes(self) -> None:
        stub = self._chains_stub()
        with self._allow({"swap": [{"chain": "ethereum", "address": ROUTER}]}):
            with mock.patch.object(actions, "chains", stub):
                entry = await actions.precheck("ethereum", 0.001, ROUTER, "swap")
        self.assertEqual(entry["address"], ROUTER)

    async def test_tx_cap_is_enforced(self) -> None:
        stub = self._chains_stub()
        with self._allow({"swap": [{"chain": "ethereum", "address": ROUTER}]}):
            with mock.patch.object(actions, "chains", stub):
                with self.assertRaises(actions.ActionError):
                    await actions.precheck("ethereum", 0.5, ROUTER, "swap")

    async def test_daily_limit_is_enforced(self) -> None:
        await db.log_action(
            {"kind": "swap", "chain": "ethereum", "ok": True, "value_eth": 0.049, "dry_run": False}
        )
        stub = self._chains_stub()
        with self._allow({"swap": [{"chain": "ethereum", "address": ROUTER}]}):
            with mock.patch.object(actions, "chains", stub):
                with self.assertRaises(actions.ActionError):
                    await actions.precheck("ethereum", 0.005, ROUTER, "swap")

    async def test_gas_cap_is_enforced(self) -> None:
        stub = self._chains_stub(gas=500.0)
        with self._allow({"swap": [{"chain": "ethereum", "address": ROUTER}]}):
            with mock.patch.object(actions, "chains", stub):
                with self.assertRaises(actions.ActionError):
                    await actions.precheck("ethereum", 0.001, ROUTER, "swap")

    async def test_empty_address_is_refused(self) -> None:
        stub = self._chains_stub(is_contract=False)
        with self._allow({"claim": [{"chain": "ethereum", "address": CLAIM_CONTRACT}]}):
            with mock.patch.object(actions, "chains", stub):
                with self.assertRaises(actions.ActionError):
                    await actions.precheck("ethereum", 0.0, CLAIM_CONTRACT, "claim")

    # ----------------------------------------------------------------- #
    async def test_claim_without_contract_refuses(self) -> None:
        result = await actions.claim("main", {"name": "NoContract", "chain": "ethereum"})
        self.assertFalse(result.ok)
        self.assertIn("claim_contract", result.message)

    async def test_claim_not_allowlisted_refuses_and_sends_nothing(self) -> None:
        stub = self._chains_stub()
        with self._allow({"claim": []}):
            with mock.patch.object(actions, "chains", stub):
                result = await actions.claim(
                    "main", {"name": "Drop", "chain": "ethereum", "claim_contract": CLAIM_CONTRACT}
                )
        self.assertFalse(result.ok)
        self.assertIn("allow-листе", result.message)
        stub.client.return_value.eth.send_raw_transaction.assert_not_called()

    async def test_dry_run_claim_returns_plan_without_broadcast(self) -> None:
        w3 = mock.MagicMock()
        w3.eth.gas_price = 5_000_000_000
        w3.eth.chain_id = 1
        w3.eth.get_transaction_count.return_value = 7
        w3.eth.contract.return_value.functions.claim.return_value.estimate_gas.return_value = 120_000
        w3.eth.contract.return_value.functions.claim.return_value.build_transaction.return_value = {"data": "0x"}
        stub = self._chains_stub(w3=w3)
        project = {"name": "Drop", "chain": "ethereum", "claim_contract": CLAIM_CONTRACT, "claim_amount": "0"}
        with self._allow({"claim": [{"chain": "ethereum", "address": CLAIM_CONTRACT}]}):
            with mock.patch.object(actions, "chains", stub):
                result = await actions.claim("main", project, confirm=True, passphrase=PASSPHRASE)
        self.assertTrue(result.ok)
        self.assertTrue(result.dry_run)
        self.assertIn("DRY-RUN", result.message)
        w3.eth.send_raw_transaction.assert_not_called()
        logged = await db.list_actions(limit=1)
        self.assertEqual(logged[0]["kind"], "claim")
        self.assertEqual(logged[0]["dry_run"], 1)

    async def test_live_mode_still_requires_confirmation(self) -> None:
        settings.dry_run = False
        w3 = mock.MagicMock()
        w3.eth.gas_price = 5_000_000_000
        w3.eth.chain_id = 1
        w3.eth.get_transaction_count.return_value = 1
        w3.eth.contract.return_value.functions.claim.return_value.estimate_gas.return_value = 100_000
        stub = self._chains_stub(w3=w3)
        project = {"name": "Drop", "chain": "ethereum", "claim_contract": CLAIM_CONTRACT}
        with self._allow({"claim": [{"chain": "ethereum", "address": CLAIM_CONTRACT}]}):
            with mock.patch.object(actions, "chains", stub):
                result = await actions.claim("main", project, confirm=False, passphrase=PASSPHRASE)
        self.assertFalse(result.ok)
        self.assertIn("Подтвердить", result.message)
        w3.eth.send_raw_transaction.assert_not_called()

    async def test_transfer_to_own_wallet_dry_run(self) -> None:
        wallet.create_wallet("second", PASSPHRASE)
        w3 = mock.MagicMock()
        w3.eth.gas_price = 5_000_000_000
        w3.eth.chain_id = 1
        w3.eth.get_transaction_count.return_value = 1
        w3.eth.estimate_gas.return_value = 21_000
        stub = self._chains_stub(w3=w3)
        with self._allow({"transfer": []}):
            with mock.patch.object(actions, "chains", stub):
                result = await actions.transfer_to_own("main", "ethereum", "second", 0.001, passphrase=PASSPHRASE)
        self.assertTrue(result.ok)
        self.assertIn("DRY-RUN", result.message)
        w3.eth.send_raw_transaction.assert_not_called()


if __name__ == "__main__":
    unittest.main()
