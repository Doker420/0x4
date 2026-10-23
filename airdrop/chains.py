"""Thin Web3 wrapper: balances, gas, receipts. Read-mostly by design."""

from __future__ import annotations

from typing import Any, Dict, Optional

from web3 import Web3
from web3.exceptions import BadFunctionCallOutput, ContractLogicError
from web3.providers.rpc import HTTPProvider

from .settings import ERC20_ABI, settings

_CLIENTS: Dict[str, Web3] = {}


class ChainError(RuntimeError):
    pass


def client(chain: str) -> Web3:
    url = settings.rpc_url(chain)
    if not url:
        raise ChainError(f"Нет RPC для сети '{chain}'. Задайте RPC_{chain.upper()} в .env.")
    if chain not in _CLIENTS:
        request_kwargs = {"timeout": settings.request_timeout}
        _CLIENTS[chain] = Web3(HTTPProvider(url, request_kwargs=request_kwargs))
    return _CLIENTS[chain]


def is_connected(chain: str) -> bool:
    try:
        return bool(client(chain).is_connected())
    except Exception:
        return False


def native_balance(chain: str, address: str) -> float:
    w3 = client(chain)
    try:
        checksum = Web3.to_checksum_address(address)
    except ValueError as exc:
        raise ChainError(f"Некорректный адрес: {address}") from exc
    wei = w3.eth.get_balance(checksum)
    return float(w3.from_wei(wei, "ether"))


def token_balance(chain: str, token: str, address: str) -> Dict[str, Any]:
    w3 = client(chain)
    try:
        contract = w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)
        raw = contract.functions.balanceOf(Web3.to_checksum_address(address)).call()
        symbol, decimals = "", 18
        try:
            symbol = contract.functions.symbol().call()
            decimals = contract.functions.decimals().call()
        except (ContractLogicError, BadFunctionCallOutput, Exception):  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        raise ChainError(f"Не удалось прочитать баланс токена: {exc}") from exc
    return {
        "raw": int(raw),
        "value": raw / (10 ** int(decimals or 18)),
        "symbol": symbol,
        "decimals": int(decimals or 18),
        "token": token,
    }


def gas_gwei(chain: str) -> float:
    w3 = client(chain)
    try:
        return float(w3.from_wei(w3.eth.gas_price, "gwei"))
    except Exception as exc:  # noqa: BLE001
        raise ChainError(f"Не удалось получить gas price: {exc}") from exc


def tx_status(chain: str, tx_hash: str) -> Dict[str, Any]:
    w3 = client(chain)
    try:
        receipt = w3.eth.get_transaction_receipt(tx_hash)
    except Exception as exc:  # noqa: BLE001
        return {"found": False, "error": str(exc)}
    return {
        "found": True,
        "status": receipt.get("status"),
        "block": receipt.get("blockNumber"),
        "gas_used": receipt.get("gasUsed"),
    }


def explorer_tx_url(chain: str, tx_hash: str) -> str:
    mapping = {
        "ethereum": "https://etherscan.io/tx/",
        "arbitrum": "https://arbiscan.io/tx/",
        "base": "https://basescan.org/tx/",
        "optimism": "https://optimistic.etherscan.io/tx/",
        "polygon": "https://polygonscan.com/tx/",
        "bsc": "https://bscscan.com/tx/",
        "linea": "https://lineascan.build/tx/",
        "scroll": "https://scrollscan.com/tx/",
        "blast": "https://blastscan.io/tx/",
        "zksync": "https://explorer.zksync.io/tx/",
    }
    prefix = mapping.get(chain, "")
    return f"{prefix}{tx_hash}" if prefix else tx_hash


def address_url(chain: str, address: str) -> str:
    mapping = {
        "ethereum": "https://etherscan.io/address/",
        "arbitrum": "https://arbiscan.io/address/",
        "base": "https://basescan.org/address/",
        "optimism": "https://optimistic.etherscan.io/address/",
        "polygon": "https://polygonscan.com/address/",
        "bsc": "https://bscscan.com/address/",
        "linea": "https://lineascan.build/address/",
        "scroll": "https://scrollscan.com/address/",
        "blast": "https://blastscan.io/address/",
        "zksync": "https://explorer.zksync.io/address/",
    }
    prefix = mapping.get(chain, "")
    return f"{prefix}{address}" if prefix else address


def is_contract(chain: str, address: str) -> Optional[bool]:
    try:
        code = client(chain).eth.get_code(Web3.to_checksum_address(address))
    except Exception:  # noqa: BLE001
        return None
    return bool(code)
