from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional

from pyrogram import Client
from pyrogram.errors import SessionPasswordNeeded

from . import db
from .config import DEFAULT_FARM_SETTINGS, normalize_media_bias

log = logging.getLogger("web.manager")
ROOT = db.ROOT
SESSIONS_DIR = ROOT / "sessions"
SESSIONS_DIR.mkdir(exist_ok=True)
SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
AUTH_TTL_SECONDS = 10 * 60


def validate_session_name(name: str) -> str:
    clean = (name or "").strip()
    if not SESSION_NAME_RE.fullmatch(clean):
        raise ValueError("Имя аккаунта: только латинские буквы, цифры, '_' и '-', максимум 64 символа")
    return clean


def parse_proxy(proxy_str: Optional[str]) -> Optional[Dict[str, Any]]:
    """Parse supported proxy strings without evaluating user-supplied code."""
    if not proxy_str or not proxy_str.strip():
        return None
    value = proxy_str.strip()

    if value.startswith("{") and value.endswith("}"):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("Прокси JSON имеет неверный формат") from exc
        if not isinstance(parsed, dict):
            raise ValueError("Ожидается объект прокси")
        scheme = str(parsed.get("scheme", "socks5")).lower()
        host = str(parsed.get("hostname", "")).strip()
        try:
            port = int(parsed.get("port", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("Порт прокси должен быть числом") from exc
        result: Dict[str, Any] = {"scheme": scheme, "hostname": host, "port": port}
        for key in ("username", "password"):
            if parsed.get(key) is not None:
                result[key] = str(parsed[key])
        return _validate_proxy(result)

    if "://" in value:
        try:
            url = urllib.parse.urlparse(value)
            scheme = url.scheme.lower()
            host = url.hostname or ""
            port = url.port or (1080 if scheme.startswith("socks") else 8080)
        except ValueError as exc:
            raise ValueError("Неверный адрес прокси") from exc
        if scheme not in {"socks4", "socks5", "http", "https"}:
            raise ValueError("Поддерживаются socks4, socks5, http и https")
        result = {
            "scheme": "http" if scheme == "https" else scheme,
            "hostname": host,
            "port": int(port),
        }
        if url.username is not None:
            result["username"] = urllib.parse.unquote(url.username)
        if url.password is not None:
            result["password"] = urllib.parse.unquote(url.password)
        return _validate_proxy(result)

    parts = value.split(":", 3)
    if len(parts) in (2, 4) and parts[1].strip().isdigit():
        host, port = parts[0].strip(), int(parts[1].strip())
        result = {"scheme": "socks5", "hostname": host, "port": port}
        if len(parts) == 4:
            result["username"] = parts[2].strip()
            result["password"] = parts[3].strip()
        return _validate_proxy(result)

    raise ValueError("Формат прокси: socks5://user:pass@host:port или host:port:user:pass")


def _validate_proxy(proxy: Dict[str, Any]) -> Dict[str, Any]:
    if not proxy.get("hostname"):
        raise ValueError("Не указан адрес прокси")
    if proxy.get("scheme") not in {"socks4", "socks5", "http"}:
        raise ValueError("Неподдерживаемый тип прокси")
    try:
        port = int(proxy.get("port", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("Порт прокси должен быть числом") from exc
    if not 1 <= port <= 65535:
        raise ValueError("Порт прокси должен быть от 1 до 65535")
    proxy["port"] = port
    return proxy


async def test_proxy_connection(proxy_val: str | Dict[str, Any]) -> Dict[str, Any]:
    """Check that a proxy TCP endpoint can be reached (does not test credentials)."""
    try:
        parsed = parse_proxy(proxy_val) if isinstance(proxy_val, str) else _validate_proxy(dict(proxy_val))
        if not parsed:
            return {"ok": False, "error": "Укажите адрес прокси"}
    except (ValueError, TypeError) as exc:
        return {"ok": False, "error": str(exc)}

    host, port = str(parsed["hostname"]), int(parsed["port"])
    started = time.monotonic()
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=6.0)
        latency_ms = round((time.monotonic() - started) * 1000, 1)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return {
            "ok": True,
            "host": host,
            "port": port,
            "scheme": parsed.get("scheme", "socks5"),
            "latency_ms": latency_ms,
        }
    except Exception as exc:
        return {"ok": False, "host": host, "port": port, "error": str(exc)[:200]}


def _client_kwargs(
    name: str,
    api_id: int,
    api_hash: str,
    phone: str = "",
    proxy: str = "",
) -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {
        "name": name,
        "api_id": int(api_id),
        "api_hash": str(api_hash),
        "workdir": str(SESSIONS_DIR),
    }
    if phone:
        kwargs["phone_number"] = phone
    if proxy and proxy.strip():
        kwargs["proxy"] = parse_proxy(proxy)
    return kwargs


def _user_info(user: Any, name: str, proxy: str = "") -> Dict[str, Any]:
    return {
        "name": name,
        "tg_id": getattr(user, "id", None),
        "username": getattr(user, "username", None),
        "first_name": getattr(user, "first_name", None),
        "phone": getattr(user, "phone_number", None) or "",
        "proxy": proxy,
    }


class AccountManager:
    """Owns authorized Pyrogram clients and verifies session files safely."""

    def __init__(self) -> None:
        self._clients: Dict[str, Client] = {}
        self._lock = asyncio.Lock()

    async def get_client(self, name: str) -> Client:
        name = validate_session_name(name)
        async with self._lock:
            cached = self._clients.get(name)
            if cached and cached.is_connected:
                return cached
            if cached:
                self._clients.pop(name, None)

            account = await db.get_account(name)
            if not account:
                raise RuntimeError(f"Аккаунт {name} не найден в базе")
            if not account.get("api_id") or not account.get("api_hash"):
                raise RuntimeError(f"Для аккаунта {name} не заданы API ID/API Hash")

            client = Client(**_client_kwargs(
                name,
                int(account["api_id"]),
                str(account["api_hash"]),
                account.get("phone") or "",
                account.get("proxy") or "",
            ))
            try:
                # Do not call start() on an unregistered session: start() may wait for
                # terminal input. Web workers must fail fast and never prompt in logs.
                authorized = await client.connect()
                if not authorized:
                    raise RuntimeError("Сессия Telegram не авторизована")
                me = await client.get_me()
                if me is None:
                    raise RuntimeError("Telegram не вернул профиль авторизованной сессии")
            except Exception as exc:
                await self._disconnect(client)
                await db.upsert_account(name, session_status="unauthorized", last_checked_at=time.time())
                raise RuntimeError(
                    f"Сессия аккаунта {name} не авторизована или недоступна. "
                    "Подключите её заново на странице «Аккаунты»."
                ) from exc

            self._clients[name] = client
            await self._save_identity(name, me)
            return client

    async def adopt_client(self, name: str, client: Client) -> None:
        name = validate_session_name(name)
        async with self._lock:
            old = self._clients.get(name)
            if old is not None and old is not client:
                await self._disconnect(old)
            self._clients[name] = client

    async def close(self, name: Optional[str] = None) -> None:
        async with self._lock:
            names = [name] if name else list(self._clients.keys())
            for account_name in names:
                client = self._clients.pop(account_name, None)
                if client is not None:
                    await self._disconnect(client)

    @staticmethod
    async def _disconnect(client: Client) -> None:
        try:
            if getattr(client, "is_initialized", False):
                await client.stop()
            elif client.is_connected:
                await client.disconnect()
        except Exception:
            log.debug("Ошибка отключения Pyrogram-клиента", exc_info=True)

    async def _save_identity(self, name: str, user: Any) -> None:
        await db.upsert_account(
            name,
            tg_id=getattr(user, "id", None),
            username=getattr(user, "username", None),
            first_name=getattr(user, "first_name", None),
            session_status="authorized",
            last_checked_at=time.time(),
        )

    async def default_api_credentials(self) -> tuple[int | None, str]:
        """Shared Telegram app credentials used to import local session files."""
        raw_api_id = await db.get_setting("telegram_api_id", "")
        raw_api_hash = await db.get_setting("telegram_api_hash", "")
        raw_api_id = raw_api_id or os.getenv("TELEGRAM_API_ID") or os.getenv("API_ID", "")
        api_hash = raw_api_hash or os.getenv("TELEGRAM_API_HASH") or os.getenv("API_HASH", "")
        try:
            api_id = int(raw_api_id)
            if api_id <= 0:
                api_id = None
        except (TypeError, ValueError):
            api_id = None
        return api_id, str(api_hash or "").strip()

    async def list_active(self) -> List[str]:
        return [name for name, client in self._clients.items() if client.is_connected]

    async def verify_session(
        self,
        name: str,
        api_id: int,
        api_hash: str,
        phone: str = "",
        proxy: str = "",
    ) -> Dict[str, Any]:
        name = validate_session_name(name)
        session_path = SESSIONS_DIR / f"{name}.session"
        if not session_path.is_file():
            raise FileNotFoundError(f"Файл сессии {name}.session не найден в sessions/")
        client = Client(**_client_kwargs(name, api_id, api_hash, phone, proxy))
        try:
            authorized = await client.connect()
            if not authorized:
                raise RuntimeError("Сессия не авторизована; войдите по номеру телефона и коду")
            user = await client.get_me()
            if user is None:
                raise RuntimeError("Сессия не авторизована; войдите по номеру телефона и коду")
            result = _user_info(user, name, proxy)
            await db.upsert_account(
                name,
                api_id=int(api_id),
                api_hash=str(api_hash),
                phone=phone or result.get("phone", ""),
                proxy=proxy.strip() if proxy else "",
                tg_id=result["tg_id"],
                username=result["username"],
                first_name=result["first_name"],
                session_status="authorized",
                last_checked_at=time.time(),
                enabled=1,
            )
            await db.add_session(name, str(session_path))
            return result
        except Exception:
            if await db.get_account(name):
                await db.upsert_account(name, session_status="unauthorized", last_checked_at=time.time())
            raise
        finally:
            await self._disconnect(client)

    async def scan_sessions_dir(self) -> List[dict]:
        """Import and verify local sessions using per-account or shared credentials."""
        results: List[dict] = []
        cfg_accounts: Dict[str, dict] = {}
        cfg_path = ROOT / "farm_config.json"
        if cfg_path.exists():
            try:
                raw = json.loads(cfg_path.read_text(encoding="utf-8"))
                for entry in raw.get("accounts", []):
                    if isinstance(entry, dict) and entry.get("name"):
                        cfg_accounts[str(entry["name"])] = entry
            except Exception as exc:
                log.warning("Не удалось прочитать локальный farm_config.json (%s)", type(exc).__name__)

        default_api_id, default_api_hash = await self.default_api_credentials()
        for path in sorted(SESSIONS_DIR.glob("*.session")):
            if not path.is_file() or not SESSION_NAME_RE.fullmatch(path.stem):
                continue
            name = path.stem
            await db.add_session(name, str(path))
            account = await db.get_account(name)
            legacy = cfg_accounts.get(name, {})

            # Prefer credentials stored for this account, then the legacy local
            # config, and finally the shared defaults entered once in the panel.
            legacy_api_id = legacy.get("api_id") or default_api_id
            legacy_api_hash = legacy.get("api_hash") or default_api_hash
            if not account and legacy_api_id and legacy_api_hash:
                has_legacy_credentials = bool(legacy.get("api_id") and legacy.get("api_hash"))
                await db.upsert_account(
                    name,
                    api_id=int(legacy_api_id),
                    api_hash=str(legacy_api_hash),
                    phone=str(legacy.get("phone") or ""),
                    proxy=str(legacy.get("proxy") or ""),
                    persona=str(legacy.get("persona") or ""),
                    reply_probability=float(legacy.get("reply_probability", DEFAULT_FARM_SETTINGS["default_reply_probability"])),
                    media_bias=json.dumps(normalize_media_bias(legacy.get("media_bias")), ensure_ascii=False),
                    behavior_customized=1 if has_legacy_credentials else 0,
                    enabled=1,
                    session_status="unknown",
                )
                account = await db.get_account(name)

            if account:
                credential_updates: Dict[str, Any] = {}
                if not account.get("api_id") and legacy_api_id:
                    credential_updates["api_id"] = int(legacy_api_id)
                if not account.get("api_hash") and legacy_api_hash:
                    credential_updates["api_hash"] = str(legacy_api_hash)
                if credential_updates:
                    await db.upsert_account(name, **credential_updates)
                    account.update(credential_updates)

            if not account or not account.get("api_id") or not account.get("api_hash"):
                results.append({"name": name, "source": "sessions/", "status": "needs_credentials"})
                continue

            try:
                info = await self.verify_session(
                    name,
                    int(account["api_id"]),
                    str(account["api_hash"]),
                    str(account.get("phone") or legacy.get("phone") or ""),
                    str(account.get("proxy") or legacy.get("proxy") or ""),
                )
                results.append({
                    "name": name,
                    "source": "sessions/",
                    "status": "authorized",
                    "username": info.get("username"),
                    "first_name": info.get("first_name"),
                })
            except Exception as exc:
                log.info("Session validation failed for %s (%s)", name, type(exc).__name__)
                results.append({"name": name, "source": "sessions/", "status": "unauthorized"})
        return results

    async def update_account_proxy(self, name: str, proxy: str) -> Dict[str, Any]:
        name = validate_session_name(name)
        account = await db.get_account(name)
        if not account:
            raise ValueError(f"Аккаунт {name} не найден")
        proxy_clean = proxy.strip() if proxy else ""
        if proxy_clean:
            parse_proxy(proxy_clean)
        await self.close(name)
        await db.upsert_account(name, proxy=proxy_clean)
        test_result = await test_proxy_connection(proxy_clean) if proxy_clean else {
            "ok": True, "message": "Прямое подключение"
        }
        return {"ok": True, "proxy": proxy_clean, "test": test_result}


class SessionAuthFlow:
    """Interactive Telegram authorization managed through web form steps."""

    def __init__(self, account_manager: AccountManager) -> None:
        self.account_manager = account_manager
        self._pending: Dict[str, Dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def start(
        self,
        name: str,
        api_id: int,
        api_hash: str,
        phone: str,
        proxy: str = "",
        persona: str = "",
        reply_probability: float = 0.85,
        media_bias: Any = None,
        behavior_customized: int = 0,
    ) -> Dict[str, Any]:
        name = validate_session_name(name)
        phone = (phone or "").strip()
        if not phone:
            raise ValueError("Укажите номер телефона в международном формате")
        if int(api_id) <= 0 or not str(api_hash).strip():
            raise ValueError("Укажите корректные API ID и API Hash из my.telegram.org")
        probability = float(reply_probability)
        if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
            raise ValueError("Вероятность ответа должна быть от 0 до 1")
        proxy = (proxy or "").strip()
        if proxy:
            parse_proxy(proxy)

        async with self._lock:
            await self._cancel_locked(name)
            await self.account_manager.close(name)
            client = Client(**_client_kwargs(name, int(api_id), str(api_hash).strip(), phone, proxy))
            try:
                authorized = await client.connect()
                credentials = {
                    "api_id": int(api_id),
                    "api_hash": str(api_hash).strip(),
                    "phone": phone,
                    "proxy": proxy,
                    "persona": persona.strip(),
                    "reply_probability": probability,
                    "media_bias": normalize_media_bias(media_bias),
                    "behavior_customized": 1 if behavior_customized else 0,
                }
                if authorized:
                    me = await client.get_me()
                    await self._finish(name, client, me, credentials)
                    return {"status": "authorized", "info": _user_info(me, name, proxy)}

                sent_code = await client.send_code(phone)
                self._pending[name] = {
                    "client": client,
                    "api_id": int(api_id),
                    "api_hash": str(api_hash).strip(),
                    "phone": phone,
                    "proxy": proxy,
                    "persona": persona.strip(),
                    "reply_probability": probability,
                    "media_bias": normalize_media_bias(media_bias),
                    "behavior_customized": 1 if behavior_customized else 0,
                    "phone_code_hash": sent_code.phone_code_hash,
                    "expires_at": time.monotonic() + AUTH_TTL_SECONDS,
                }
                return {"status": "code_required", "phone": _mask_phone(phone)}
            except Exception:
                await self.account_manager._disconnect(client)
                raise

    async def submit_code(self, name: str, code: str) -> Dict[str, Any]:
        name = validate_session_name(name)
        async with self._lock:
            pending = await self._require_pending(name)
            try:
                user = await pending["client"].sign_in(
                    pending["phone"], pending["phone_code_hash"], (code or "").strip()
                )
            except SessionPasswordNeeded:
                return {"status": "password_required"}
            except Exception:
                raise
            await self._finish(name, pending["client"], user, pending)
            return {"status": "authorized", "info": _user_info(user, name, pending["proxy"])}

    async def submit_password(self, name: str, password: str) -> Dict[str, Any]:
        name = validate_session_name(name)
        async with self._lock:
            pending = await self._require_pending(name)
            user = await pending["client"].check_password(password)
            await self._finish(name, pending["client"], user, pending)
            return {"status": "authorized", "info": _user_info(user, name, pending["proxy"])}

    async def cancel(self, name: str) -> None:
        name = validate_session_name(name)
        async with self._lock:
            await self._cancel_locked(name)

    async def close_all(self) -> None:
        async with self._lock:
            names = list(self._pending)
            for name in names:
                await self._cancel_locked(name)

    async def _require_pending(self, name: str) -> Dict[str, Any]:
        pending = self._pending.get(name)
        if not pending:
            raise ValueError("Шаг авторизации истёк или не найден. Начните подключение заново.")
        if time.monotonic() > pending["expires_at"]:
            await self._cancel_locked(name)
            raise ValueError("Код авторизации истёк. Начните подключение заново.")
        return pending

    async def _finish(self, name: str, client: Client, user: Any, credentials: Dict[str, Any]) -> None:
        if user is None or getattr(user, "id", None) is None:
            user = await client.get_me()
        if user is None:
            raise RuntimeError("Telegram не подтвердил авторизацию")
        await db.upsert_account(
            name,
            api_id=int(credentials["api_id"]),
            api_hash=str(credentials["api_hash"]),
            phone=str(credentials.get("phone") or getattr(user, "phone_number", "") or ""),
            proxy=str(credentials.get("proxy") or ""),
            persona=str(credentials.get("persona") or ""),
            media_bias=json.dumps(normalize_media_bias(credentials.get("media_bias")), ensure_ascii=False),
            reply_probability=float(credentials.get("reply_probability", 0.85)),
            behavior_customized=1 if credentials.get("behavior_customized") else 0,
            enabled=1,
            tg_id=getattr(user, "id", None),
            username=getattr(user, "username", None),
            first_name=getattr(user, "first_name", None),
            session_status="authorized",
            last_checked_at=time.time(),
        )
        await db.add_session(name, str(SESSIONS_DIR / f"{name}.session"))
        self._pending.pop(name, None)
        await self.account_manager.adopt_client(name, client)

    async def _cancel_locked(self, name: str) -> None:
        pending = self._pending.pop(name, None)
        if pending:
            await self.account_manager._disconnect(pending["client"])


def _mask_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    if len(digits) < 5:
        return "номер телефона"
    return f"+{'*' * max(0, len(digits) - 4)}{digits[-4:]}"


async def import_session_file(
    filename: str,
    api_id: int,
    api_hash: str,
    phone: Optional[str] = None,
    proxy: Optional[str] = None,
) -> Dict[str, Any]:
    """Verify an existing Pyrogram session and register it in the web panel."""
    basename = Path(filename).name
    if basename != filename or not basename.endswith(".session"):
        raise ValueError("Ожидается имя файла вида <account>.session")
    name = validate_session_name(Path(basename).stem)
    return await manager.verify_session(name, api_id, api_hash, phone or "", proxy or "")


manager = AccountManager()
auth_flow = SessionAuthFlow(manager)
