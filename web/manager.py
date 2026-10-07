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


class SessionNotAuthorizedError(RuntimeError):
    """Raised when a session is positively known to have no active authorization."""


class SessionVerificationError(RuntimeError):
    """The current state could not be confirmed; keep the last-known account status."""


class SessionBusyError(RuntimeError):
    """The session file is in use by another panel operation or the chat farm."""


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) is not a harmless probe on Windows: non-console signals
        # can terminate the process. Query its handle instead.
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            error = ctypes.get_last_error()
            return error != 87  # ERROR_INVALID_PARAMETER means no such process
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True
            return exit_code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        # An inability to verify the PID is not evidence that its sessions are free.
        return True


def farm_process_running() -> bool:
    """Detect a farm launched outside this web worker using its PID marker."""
    lock_path = db.DATA_DIR / "farm.lock"
    try:
        raw_pid = lock_path.read_text(encoding="ascii").strip()
        pid = int(raw_pid)
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        log.warning("Ignoring malformed farm lock marker %s", lock_path)
        return False
    if _pid_is_running(pid):
        return True
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass
    return False


_SESSION_UNAUTHORIZED_ERROR_NAMES = {
    "unauthorized",
    "authkeyunregistered",
    "authkeyinvalid",
    "sessionrevoked",
    "sessionexpired",
}


def _is_explicitly_unauthorized(exc: BaseException) -> bool:
    return any(
        cls.__name__.casefold() in _SESSION_UNAUTHORIZED_ERROR_NAMES
        for cls in type(exc).__mro__
    )


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
        self._busy_sessions: set[str] = set()
        self._farm_reserved = False
        self._lock = asyncio.Lock()
        self._scan_lock = asyncio.Lock()

    def farm_sessions_busy(self) -> bool:
        return self._farm_reserved or farm_process_running()

    async def prepare_for_farm(self) -> None:
        """Close web-owned clients before farm.py takes ownership of .session files."""
        # Let any in-progress full-folder scan finish before reserving the session files.
        async with self._scan_lock:
            async with self._lock:
                if self._farm_reserved or farm_process_running():
                    raise SessionBusyError("Чат-ферма уже запущена или запускается")
                if self._busy_sessions:
                    names = ", ".join(sorted(self._busy_sessions))
                    raise SessionBusyError(f"Завершите или отмените вход для аккаунтов: {names}")
                self._farm_reserved = True
                clients = list(self._clients.values())
                self._clients.clear()
                for client in clients:
                    await self._disconnect(client)

    async def finish_farm(self) -> None:
        async with self._lock:
            self._farm_reserved = False

    async def get_client(self, name: str) -> Client:
        name = validate_session_name(name)
        async with self._lock:
            if name in self._busy_sessions:
                raise SessionBusyError(f"Сессия {name} сейчас используется в процессе входа")
            if self.farm_sessions_busy():
                raise SessionBusyError("Сессии заняты запущенной чат-фермой")

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
            previously_authorized = account.get("session_status") == "authorized"
            try:
                # Do not call start() on an unregistered session: start() may wait for
                # terminal input. Web workers must fail fast and never prompt in logs.
                authorized = await client.connect()
                if not authorized:
                    if previously_authorized:
                        raise SessionVerificationError(
                            "Telegram не подтвердил сохранённую сессию; прежний статус авторизации сохранён"
                        )
                    raise SessionNotAuthorizedError("Сессия Telegram не авторизована")
                me = await client.get_me()
                if me is None:
                    if previously_authorized:
                        raise SessionVerificationError(
                            "Не удалось подтвердить сохранённую сессию; прежний статус авторизации сохранён"
                        )
                    raise SessionNotAuthorizedError("Telegram не подтвердил авторизацию сессии")
            except SessionVerificationError:
                await self._disconnect(client)
                raise
            except SessionNotAuthorizedError:
                await self._disconnect(client)
                await db.upsert_account(name, session_status="unauthorized", last_checked_at=time.time())
                raise
            except Exception as exc:
                await self._disconnect(client)
                if _is_explicitly_unauthorized(exc):
                    await db.upsert_account(name, session_status="unauthorized", last_checked_at=time.time())
                    raise SessionNotAuthorizedError(
                        "Telegram сообщил, что сессия больше не авторизована"
                    ) from exc
                # Network errors, Telegram rate limits, bad app credentials and
                # SQLite session-file locks do not prove that a valid session expired.
                raise

            try:
                await self._save_identity(name, me)
            except Exception:
                await self._disconnect(client)
                raise
            self._clients[name] = client
            return client

    async def reserve_for_auth(self, name: str) -> None:
        """Mark a session busy before an interactive login opens its SQLite file."""
        name = validate_session_name(name)
        async with self._lock:
            if self.farm_sessions_busy():
                raise SessionBusyError("Сессии заняты запущенной чат-фермой")
            self._busy_sessions.add(name)
            old = self._clients.pop(name, None)
            if old is not None:
                await self._disconnect(old)

    async def release_auth_reservation(self, name: str) -> None:
        name = validate_session_name(name)
        async with self._lock:
            self._busy_sessions.discard(name)

    async def adopt_client(self, name: str, client: Client) -> None:
        name = validate_session_name(name)
        async with self._lock:
            old = self._clients.get(name)
            if old is not None and old is not client:
                await self._disconnect(old)
            self._clients[name] = client
            self._busy_sessions.discard(name)

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

        async with self._lock:
            if name in self._busy_sessions:
                raise SessionBusyError(f"Сессия {name} занята незавершённым входом")
            if self.farm_sessions_busy():
                raise SessionBusyError("Сессии заняты запущенной чат-фермой")

            account = await db.get_account(name)
            previously_authorized = bool(account and account.get("session_status") == "authorized")
            cached = self._clients.get(name)
            cached_credentials_match = bool(
                cached
                and cached.is_connected
                and account
                and account.get("api_id")
                and account.get("api_hash")
                and int(account["api_id"]) == int(api_id)
                and str(account["api_hash"]) == str(api_hash)
                and str(account.get("proxy") or "").strip() == str(proxy or "").strip()
                and (not phone or str(account.get("phone") or "").strip() == str(phone).strip())
            )
            use_cached = cached_credentials_match
            if cached is not None and not use_cached:
                self._clients.pop(name, None)
                await self._disconnect(cached)

            client = cached if use_cached else Client(**_client_kwargs(name, api_id, api_hash, phone, proxy))
            try:
                if use_cached:
                    # Never open a second Pyrogram SQLite connection to the same
                    # .session file; reuse the account's already-live client.
                    user = await client.get_me()
                else:
                    # Do not call start() on an unregistered session: start() may
                    # wait for terminal input in a headless web worker.
                    authorized = await client.connect()
                    if not authorized:
                        if previously_authorized:
                            raise SessionVerificationError(
                                "Telegram не подтвердил сохранённую сессию; прежний статус авторизации сохранён"
                            )
                        raise SessionNotAuthorizedError(
                            "Сессия не авторизована; войдите по номеру телефона и коду"
                        )
                    user = await client.get_me()

                if user is None:
                    if previously_authorized:
                        raise SessionVerificationError(
                            "Не удалось подтвердить сохранённую сессию; прежний статус авторизации сохранён"
                        )
                    raise SessionNotAuthorizedError(
                        "Сессия не авторизована; войдите по номеру телефона и коду"
                    )

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
            except SessionVerificationError:
                # A local DB/connection problem is not a Telegram logout. Keep
                # the last known authorization state and the original session file.
                raise
            except SessionNotAuthorizedError:
                if use_cached:
                    self._clients.pop(name, None)
                    await self._disconnect(client)
                if await db.get_account(name):
                    await db.upsert_account(name, session_status="unauthorized", last_checked_at=time.time())
                raise
            except Exception as exc:
                if _is_explicitly_unauthorized(exc):
                    if use_cached:
                        self._clients.pop(name, None)
                        await self._disconnect(client)
                    if await db.get_account(name):
                        await db.upsert_account(name, session_status="unauthorized", last_checked_at=time.time())
                    raise SessionNotAuthorizedError(
                        "Telegram сообщил, что сессия больше не авторизована"
                    ) from exc
                # Network errors, Telegram rate limits, SQLite locks, and app
                # credential problems preserve the last-known authorization status.
                raise
            finally:
                if not use_cached:
                    await self._disconnect(client)

    async def scan_sessions_dir(self) -> List[dict]:
        """Scan once at a time; never touch session files while the farm owns them."""
        if self.farm_sessions_busy():
            raise SessionBusyError("Сессии заняты запущенной чат-фермой")
        async with self._scan_lock:
            return await self._scan_sessions_dir_locked()

    async def _scan_sessions_dir_locked(self) -> List[dict]:
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
            except SessionNotAuthorizedError as exc:
                log.info("Session is logged out for %s", name)
                if await db.get_account(name):
                    await db.upsert_account(name, session_status="unauthorized", last_checked_at=time.time())
                results.append({"name": name, "source": "sessions/", "status": "unauthorized", "error": str(exc)[:200]})
            except Exception as exc:
                log.info("Session check temporarily failed for %s (%s)", name, type(exc).__name__)
                results.append({
                    "name": name,
                    "source": "sessions/",
                    "status": "verification_failed",
                    "error": str(exc).strip()[:200] or type(exc).__name__,
                })
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
            await self.account_manager.reserve_for_auth(name)
            client: Optional[Client] = None
            try:
                client = Client(**_client_kwargs(name, int(api_id), str(api_hash).strip(), phone, proxy))
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
                if client is not None:
                    await self.account_manager._disconnect(client)
                await self.account_manager.release_auth_reservation(name)
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
        await self.account_manager.release_auth_reservation(name)


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
