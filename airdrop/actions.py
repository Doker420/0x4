"""Guarded on-chain actions: claim, swap, transfer to your own address.

Safety model (fail closed)
--------------------------
1. ``AIRDROP_DRY_RUN=1`` by default → every action is simulated and returns a
   plan instead of a transaction.
2. Only contracts listed in ``config/contracts.json`` (or ``config/airdrops.json``)
   may be touched, unless ``AIRDROP_ALLOW_UNKNOWN_CONTRACTS=1``.
3. The target must actually contain bytecode on that chain (typo guard).
4. Hard caps: ``AIRDROP_MAX_TX_VALUE_ETH``, ``AIRDROP_DAILY_LIMIT_ETH``,
   ``AIRDROP_MAX_GAS_GWEI``.
5. Anything above ``AIRDROP_CONFIRM_ABOVE_ETH`` needs ``confirm=True`` (the bot
   asks the operator to press a button first).
6. Funds can only be sent to addresses that belong to the operator's keystore.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from eth_account.signers.local import LocalAccount
from web3 import Web3

from . import chains, db, wallet
from .models import ActionResult
from .settings import CLAIM_ABI, settings

log = logging.getLogger("airdrop.actions")

SWAP_ABI = [
    {
        "name": "swapExactETHForTokens",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [
            {"name": "amountOutMin", "type": "uint256"},
            {"name": "path", "type": "address[]"},
            {"name": "to", "type": "address"},
            {"name": "deadline", "type": "uint256"},
        ],
        "outputs": [{"name": "amounts", "type": "uint256[]"}],
    },
    {
        "name": "getAmountsOut",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "amountIn", "type": "uint256"},
            {"name": "path", "type": "address[]"},
        ],
        "outputs": [{"name": "amounts", "type": "uint256[]"}],
    },
]

ERC20_APPROVE_ABI = [
    {
        "name": "approve",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    }
]

WETH_ADDRESSES = {
    "ethereum": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
    "arbitrum": "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
    "base": "0x4200000000000000000000000000000000000006",
    "optimism": "0x4200000000000000000000000000000000000006",
    "polygon": "0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270",
    "bsc": "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",
    "linea": "0xe5D7C2a44FfDDf6b295A15c148167daaAf5Cf34f",
    "scroll": "0x5300000000000000000000000000000000000004",
    "blast": "0x4300000000000000000000000000000000000004",
    "zksync": "0x5AEa5775959fBC2557Cc8789bC1bf90A239D9a91",
}


class ActionError(RuntimeError):
    """A refusal. The message is always safe to show the operator."""


# --------------------------------------------------------------------- #
# allow-lists
# --------------------------------------------------------------------- #
def contracts_file() -> Path:
    return Path(settings.root / "config" / "contracts.json")


def load_allowlist() -> Dict[str, Any]:
    path = contracts_file()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:  # pragma: no cover
        log.warning("cannot read %s: %s", path, exc)
        return {}


def _allowlist_entries(chain: str, kind: str) -> List[Dict[str, Any]]:
    data = load_allowlist()
    entries: List[Dict[str, Any]] = []
    for row in data.get(kind, []) if isinstance(data, dict) else []:
        if isinstance(row, dict) and row.get("chain") == chain:
            entries.append(row)
    return entries


def _is_allowed(chain: str, address: str, kind: str) -> Optional[Dict[str, Any]]:
    if settings.allow_unknown_contracts:
        return {"address": address, "chain": chain, "label": "unknown (allow_unknown_contracts=1)"}
    target = address.lower()
    for entry in _allowlist_entries(chain, kind):
        if str(entry.get("address", "")).lower() == target:
            return entry
    return None


# --------------------------------------------------------------------- #
# pre-flight
# --------------------------------------------------------------------- #
async def precheck(chain: str, value_eth: float, address: str, kind: str) -> Dict[str, Any]:
    """Validate an action before it is built. Raises ActionError on refusal."""
    if chain not in settings.rpc:
        settings.rpc_url(chain)
    if not settings.rpc_url(chain):
        raise ActionError(f"Нет RPC для сети {chain}.")
    if value_eth > settings.max_tx_value_eth:
        raise ActionError(
            f"Сумма {value_eth} ETH выше лимита на транзакцию ({settings.max_tx_value_eth} ETH)."
        )
    spent = await db.spent_today_eth()
    if spent + value_eth > settings.daily_limit_eth:
        raise ActionError(
            f"Дневной лимит исчерпан: уже {spent:.4f} ETH из {settings.daily_limit_eth} ETH."
        )
    # Allow-list first: it is the cheapest and most actionable refusal, and it
    # must not depend on the network being reachable.
    entry = _is_allowed(chain, address, kind)
    if entry is None:
        raise ActionError(
            f"Контракт {address} не в allow-листе ({kind}, сеть {chain}). "
            f"Добавьте его в config/contracts.json — так вы подтверждаете, что проверили адрес "
            f"на официальном сайте проекта."
        )
    try:
        gwei = chains.gas_gwei(chain)
    except Exception as exc:  # noqa: BLE001
        raise ActionError(f"Не удалось узнать цену газа: {exc}") from exc
    if gwei > settings.max_gas_gwei:
        raise ActionError(f"Газ {gwei:.1f} gwei выше лимита {settings.max_gas_gwei} gwei.")
    is_contract = chains.is_contract(chain, address)
    if is_contract is False:
        raise ActionError(f"На адресе {address} в сети {chain} нет контракта — проверьте адрес.")
    return entry or {}


# --------------------------------------------------------------------- #
# transaction helpers
# --------------------------------------------------------------------- #
def _send(w3: Web3, account: LocalAccount, tx: Dict[str, Any]) -> str:
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction if hasattr(signed, "raw_transaction") else signed.rawTransaction)
    return Web3.to_hex(tx_hash)


def _base_tx(w3: Web3, account: LocalAccount, value_wei: int, to: str) -> Dict[str, Any]:
    checksum = Web3.to_checksum_address(to)
    tx: Dict[str, Any] = {
        "chainId": w3.eth.chain_id,
        "from": account.address,
        "to": checksum,
        "value": int(value_wei),
        "nonce": w3.eth.get_transaction_count(account.address),
        "gasPrice": w3.eth.gas_price,
    }
    return tx


# --------------------------------------------------------------------- #
# actions
# --------------------------------------------------------------------- #
async def claim(
    wallet_name: str,
    project: Dict[str, Any],
    confirm: bool = False,
    passphrase: Optional[str] = None,
) -> ActionResult:
    """Call ``claim`` on the project's distributor contract."""
    chain = project.get("chain") or settings.default_chain
    contract = project.get("claim_contract") or ""
    name = project.get("name", "project")
    if not contract:
        return ActionResult("claim", False, project=name, message="У проекта не указан claim_contract.")

    raw_amount = str(project.get("claim_amount", "0") or "0")
    try:
        amount_wei = int(raw_amount) if raw_amount.isdigit() else Web3.to_wei(float(raw_amount), "ether")
    except (TypeError, ValueError):
        amount_wei = 0
    proof = project.get("merkle_proof") or []
    index = int(project.get("merkle_index", 0) or 0)
    value_eth = 0.0

    try:
        await precheck(chain, value_eth, contract, "claim")
    except ActionError as exc:
        result = ActionResult("claim", False, chain=chain, project=name, message=str(exc), dry_run=settings.dry_run)
        await db.log_action(result)
        return result

    account = wallet.load_account(wallet_name, passphrase)
    w3 = chains.client(chain)
    distributor = w3.eth.contract(address=Web3.to_checksum_address(contract), abi=CLAIM_ABI)

    builder = None
    for args in (
        (index, account.address, amount_wei, [Web3.to_checksum_address(p) for p in proof]) if proof else None,
        ([Web3.to_bytes(hexstr=p) for p in proof], amount_wei) if proof else None,
        (amount_wei,) if amount_wei else None,
        (),
    ):
        if args is None:
            continue
        try:
            builder = distributor.functions.claim(*args)
            builder.estimate_gas({"from": account.address, "value": 0})
            break
        except Exception:  # noqa: BLE001 - try the next ABI variant
            builder = None
            continue
    if builder is None:
        result = ActionResult(
            "claim",
            False,
            address=account.address,
            chain=chain,
            project=name,
            message="Не удалось подобрать сигнатуру claim() — транзакция НЕ отправлена. "
            "Проверьте ABI/аргументы в config/airdrops.json.",
            dry_run=settings.dry_run,
        )
        await db.log_action(result)
        return result

    tx = _base_tx(w3, account, 0, contract)
    try:
        tx["gas"] = int(builder.estimate_gas({"from": account.address}) * 1.2) + 10_000
    except Exception as exc:  # noqa: BLE001
        result = ActionResult("claim", False, address=account.address, chain=chain, project=name,
                              message=f"Оценка газа не прошла: {exc}", dry_run=settings.dry_run)
        await db.log_action(result)
        return result
    tx.update(builder.build_transaction(tx))

    if settings.dry_run:
        result = ActionResult(
            "claim", True, address=account.address, chain=chain, project=name,
            message=f"DRY-RUN: вызов claim() на {contract} ({chain}). "
            f"Для боевого запуска поставьте AIRDROP_DRY_RUN=0 в .env и перезапустите сервис.",
            dry_run=True,
        )
        await db.log_action(result)
        return result

    if not confirm:
        result = ActionResult(
            "claim", False, address=account.address, chain=chain, project=name,
            message="Требуется подтверждение: нажмите «Подтвердить» в боте.", dry_run=False,
        )
        await db.log_action(result)
        return result

    try:
        tx_hash = _send(w3, account, tx)
    except Exception as exc:  # noqa: BLE001
        result = ActionResult("claim", False, address=account.address, chain=chain, project=name,
                              message=f"Отправка не удалась: {exc}", dry_run=False)
        await db.log_action(result)
        return result
    url = chains.explorer_tx_url(chain, tx_hash)
    result = ActionResult("claim", True, address=account.address, chain=chain, project=name,
                          tx_hash=tx_hash, message=f"Клейм отправлен: {url}", dry_run=False)
    await db.log_action(result)
    return result


async def swap_eth_for_token(
    wallet_name: str,
    chain: str,
    token_out: str,
    value_eth: float,
    slippage_pct: float = 1.0,
    confirm: bool = False,
    passphrase: Optional[str] = None,
    router: str = "",
) -> ActionResult:
    """Swap native coin → ERC-20 through an allow-listed router."""
    if not router:
        entries = _allowlist_entries(chain, "swap")
        for entry in entries:
            router = str(entry.get("address", ""))
            if router:
                break
    if not router:
        return ActionResult("swap", False, chain=chain,
                            message=f"Нет allow-листнутого роутера для сети {chain}. Добавьте его в config/contracts.json.")

    try:
        await precheck(chain, value_eth, router, "swap")
    except ActionError as exc:
        result = ActionResult("swap", False, chain=chain, project=router, message=str(exc), dry_run=settings.dry_run)
        await db.log_action(result)
        return result

    account = wallet.load_account(wallet_name, passphrase)
    w3 = chains.client(chain)
    weth = WETH_ADDRESSES.get(chain)
    if not weth:
        return ActionResult("swap", False, chain=chain, message=f"Не знаю WETH-адрес для сети {chain}.")

    path = [Web3.to_checksum_address(weth), Web3.to_checksum_address(token_out)]
    contract = w3.eth.contract(address=Web3.to_checksum_address(router), abi=SWAP_ABI)
    value_wei = Web3.to_wei(value_eth, "ether")

    try:
        amounts = contract.functions.getAmountsOut(value_wei, path).call()
        min_out = int(amounts[-1] * (1 - slippage_pct / 100))
    except Exception as exc:  # noqa: BLE001
        result = ActionResult("swap", False, address=account.address, chain=chain,
                              message=f"Не удалось оценить обмен: {exc}", dry_run=settings.dry_run, value_eth=value_eth)
        await db.log_action(result)
        return result

    builder = contract.functions.swapExactETHForTokens(
        min_out, path, account.address, int(w3.eth.get_block("latest")["timestamp"]) + 600
    )
    tx = _base_tx(w3, account, value_wei, router)
    try:
        gas = builder.estimate_gas({"from": account.address, "value": value_wei})
        tx["gas"] = int(gas * 1.2) + 10_000
    except Exception as exc:  # noqa: BLE001
        result = ActionResult("swap", False, address=account.address, chain=chain,
                              message=f"Оценка газа не прошла: {exc}", dry_run=settings.dry_run, value_eth=value_eth)
        await db.log_action(result)
        return result
    tx.update(builder.build_transaction(tx))

    if settings.dry_run:
        result = ActionResult("swap", True, address=account.address, chain=chain, project=router,
                              message=f"DRY-RUN: обмен {value_eth} ETH → токен {token_out} "
                              f"(минимум к получению ≈ {min_out}). Поставьте AIRDROP_DRY_RUN=0 для боевого режима.",
                              value_eth=value_eth, dry_run=True)
        await db.log_action(result)
        return result

    if value_eth >= settings.confirm_above_eth and not confirm:
        result = ActionResult("swap", False, address=account.address, chain=chain, project=router,
                              message=f"Сумма {value_eth} ETH ≥ {settings.confirm_above_eth} ETH — нужно подтверждение в боте.",
                              value_eth=value_eth, dry_run=False)
        await db.log_action(result)
        return result

    try:
        tx_hash = _send(w3, account, tx)
    except Exception as exc:  # noqa: BLE001
        result = ActionResult("swap", False, address=account.address, chain=chain, project=router,
                              message=f"Отправка не удалась: {exc}", value_eth=value_eth, dry_run=False)
        await db.log_action(result)
        return result
    result = ActionResult("swap", True, address=account.address, chain=chain, project=router,
                          tx_hash=tx_hash, message=f"Своп отправлен: {chains.explorer_tx_url(chain, tx_hash)}",
                          value_eth=value_eth, dry_run=False)
    await db.log_action(result)
    return result


async def transfer_to_own(
    wallet_name: str,
    chain: str,
    to_wallet_name: str,
    value_eth: float,
    confirm: bool = False,
    passphrase: Optional[str] = None,
) -> ActionResult:
    """Move native coin between two of the operator's own wallets (same chain)."""
    destination = wallet.wallet_address(to_wallet_name, passphrase)
    try:
        await precheck(chain, value_eth, destination, "transfer")
    except ActionError as exc:
        if "allow-листе" not in str(exc):
            result = ActionResult("transfer", False, chain=chain, message=str(exc), dry_run=settings.dry_run)
            await db.log_action(result)
            return result
    account = wallet.load_account(wallet_name, passphrase)
    w3 = chains.client(chain)
    tx = _base_tx(w3, account, Web3.to_wei(value_eth, "ether"), destination)
    tx["gas"] = w3.eth.estimate_gas(tx)

    if settings.dry_run:
        result = ActionResult("transfer", True, address=account.address, chain=chain,
                              message=f"DRY-RUN: перевод {value_eth} ETH {account.address} → {destination}",
                              value_eth=value_eth, dry_run=True)
        await db.log_action(result)
        return result
    if value_eth >= settings.confirm_above_eth and not confirm:
        result = ActionResult("transfer", False, address=account.address, chain=chain,
                              message="Нужно подтверждение в боте.", value_eth=value_eth, dry_run=False)
        await db.log_action(result)
        return result
    try:
        tx_hash = _send(w3, account, tx)
    except Exception as exc:  # noqa: BLE001
        result = ActionResult("transfer", False, address=account.address, chain=chain,
                              message=f"Отправка не удалась: {exc}", value_eth=value_eth, dry_run=False)
        await db.log_action(result)
        return result
    result = ActionResult("transfer", True, address=account.address, chain=chain, tx_hash=tx_hash,
                          message=f"Перевод отправлен: {chains.explorer_tx_url(chain, tx_hash)}",
                          value_eth=value_eth, dry_run=False)
    await db.log_action(result)
    return result
