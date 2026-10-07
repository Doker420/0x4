from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Cookie, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import auth, chat_context, db, giphy, manager, mass_actions, tasks
from .config import (
    DEFAULT_FARM_SETTINGS,
    DEFAULT_MEDIA_BIAS,
    load_farm_settings,
    normalize_media_bias,
    parse_roulette_numbers,
)
from .deepseek import ai
from .manager import SESSIONS_DIR, auth_flow, import_session_file, validate_session_name

log = logging.getLogger("web")
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app = FastAPI(title="0x4 — управление аккаунтами")
MAX_SESSION_UPLOAD = 25 * 1024 * 1024
_PENDING_INVITE_REFERENCES: dict[str, tuple[float, dict[str, Any]]] = {}
INVITE_REFERENCE_TTL = 60 * 60


def _store_invite_reference(reference: dict[str, Any]) -> str:
    """Keep private invite hashes out of persisted task payloads and logs."""
    now = asyncio.get_running_loop().time()
    for token, (created_at, _) in list(_PENDING_INVITE_REFERENCES.items()):
        if now - created_at > INVITE_REFERENCE_TTL:
            _PENDING_INVITE_REFERENCES.pop(token, None)
    token = uuid.uuid4().hex
    _PENDING_INVITE_REFERENCES[token] = (now, dict(reference))
    return token


def _take_invite_reference(token: str) -> Optional[dict[str, Any]]:
    stored = _PENDING_INVITE_REFERENCES.pop(str(token), None)
    if not stored:
        return None
    created_at, reference = stored
    if asyncio.get_running_loop().time() - created_at > INVITE_REFERENCE_TTL:
        return None
    return dict(reference)


def _discard_invite_reference(token: Optional[str]) -> None:
    if token:
        _PENDING_INVITE_REFERENCES.pop(str(token), None)


# ─── Task handlers ───
@tasks.register("mass_join")
async def _h_mass_join(payload: dict) -> dict:
    return await mass_actions.mass_join(
        payload["accounts"], payload["target"], tuple(payload.get("delay", [5, 20]))
    )


@tasks.register("mass_bio")
async def _h_mass_bio(payload: dict) -> dict:
    return await mass_actions.mass_set_bio(payload["accounts"], payload["bio"])


@tasks.register("mass_avatar")
async def _h_mass_avatar(payload: dict) -> dict:
    return await mass_actions.mass_set_avatar(payload["accounts"], payload["photos"])


@tasks.register("create_group")
async def _h_create_group(payload: dict) -> dict:
    return await mass_actions.create_group(payload["account"], payload["title"], payload.get("members"))


@tasks.register("create_channel")
async def _h_create_channel(payload: dict) -> dict:
    return await mass_actions.create_channel(
        payload["account"], payload["title"], payload.get("about", "")
    )


@tasks.register("parse_users")
async def _h_parse_users(payload: dict) -> dict:
    users = await mass_actions.parse_users(
        payload["reader"], int(payload["chat_id"]), int(payload.get("limit", 1000))
    )
    out_path = db.DATA_DIR / f"parsed_{int(payload['chat_id'])}.json"
    out_path.write_text(json.dumps(users, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"count": len(users), "file": str(out_path.relative_to(db.ROOT))}


@tasks.register("comment_post")
async def _h_comment_post(payload: dict) -> dict:
    return await mass_actions.comment_post(payload["account"], payload["url"], payload["comment"])


@tasks.register("history_fetch")
async def _h_history_fetch(payload: dict) -> dict:
    msgs = await mass_actions.fetch_history(
        payload["reader"], payload["chat"], int(payload.get("limit", 500))
    )
    safe_chat = str(payload["chat"]).replace("/", "_")
    out_path = db.DATA_DIR / f"history_{safe_chat}.json"
    out_path.write_text(json.dumps(msgs, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"count": len(msgs), "file": str(out_path.relative_to(db.ROOT))}


@tasks.register("chat_context_collect")
async def _h_chat_context_collect(payload: dict) -> dict:
    if FARM_PROCESS is not None and FARM_PROCESS.returncode is None:
        raise RuntimeError("Остановите чат-ферму перед проверкой членства и сбором истории")
    invite_token = payload.pop("invite_token", None)
    if invite_token:
        reference = _take_invite_reference(invite_token)
        if reference is None:
            raise RuntimeError("Invite-ссылка больше недоступна в памяти. Повторите запуск сбора истории.")
        payload["reference"] = reference
    return await chat_context.collect_chat_context(payload)


FARM_PROCESS: Optional[asyncio.subprocess.Process] = None
FARM_LOG_TASK: Optional[asyncio.Task] = None
FARM_LOG_FILE = db.DATA_DIR / "farm.log"


def _current_owner(web_auth: Optional[str]) -> dict:
    return auth.require_owner(web_auth)


def _require_farm_stopped_for_session_check() -> None:
    farm_alive_in_panel = FARM_PROCESS is not None and FARM_PROCESS.returncode is None
    if farm_alive_in_panel or manager.manager.farm_sessions_busy():
        raise HTTPException(
            409,
            "Сессии заняты работающей чат-фермой. Остановите чат-ферму перед проверкой или изменением .session-файлов.",
        )


def _render(request: Request, name: str, context: dict[str, Any]) -> HTMLResponse:
    context.setdefault("request", request)
    return templates.TemplateResponse(request=request, name=name, context=context)


def _account_for_ui(row: dict[str, Any]) -> dict[str, Any]:
    bias = normalize_media_bias(row.get("media_bias"), DEFAULT_MEDIA_BIAS)
    return {
        "name": row["name"],
        "phone": row.get("phone") or "",
        "api_id": row.get("api_id") or "",
        "api_hash_set": bool(row.get("api_hash")),
        "proxy": row.get("proxy") or "",
        "persona": row.get("persona") or "",
        "media_bias": bias,
        "media_percent": {kind: round(value * 100) for kind, value in bias.items()},
        "reply_probability": float(row.get("reply_probability") or 0),
        "behavior_customized": bool(row.get("behavior_customized", 0)),
        "enabled": bool(row.get("enabled", 1)),
        "session_status": row.get("session_status") or "unknown",
        "username": row.get("username") or "",
        "first_name": row.get("first_name") or "",
        "tg_id": row.get("tg_id"),
        "last_checked_at": row.get("last_checked_at"),
    }


def _probability(value: float, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"{label}: укажите число от 0 до 1") from exc
    if not math.isfinite(result) or not 0 <= result <= 1:
        raise HTTPException(status_code=422, detail=f"{label}: укажите число от 0 до 1")
    return result


def _media_weights(text: float, gif: float, sticker: float, photo: float, voice: float) -> dict[str, float]:
    values = {"text": text, "gif": gif, "sticker": sticker, "photo": photo, "voice": voice}
    if any(not math.isfinite(float(value)) or float(value) < 0 for value in values.values()):
        raise HTTPException(status_code=422, detail="Веса медиа должны быть неотрицательными числами")
    if sum(values.values()) <= 0:
        raise HTTPException(status_code=422, detail="Сумма весов медиа должна быть больше нуля")
    return normalize_media_bias(values)


def _safe_error(exc: Exception) -> str:
    return str(exc).strip()[:300] or type(exc).__name__


# ─── Lifecycle ───
@app.on_event("startup")
async def _startup() -> None:
    await db.init_db()
    await tasks.runner.start()
    try:
        await ai.start()
    except Exception:
        log.exception("DeepSeek bridge не подключился; панель продолжит работу без генерации")


@app.on_event("shutdown")
async def _shutdown() -> None:
    global FARM_PROCESS, FARM_LOG_TASK
    await tasks.runner.stop()
    process = FARM_PROCESS
    if process is not None and process.returncode is None:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=8.0)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
    if FARM_LOG_TASK is not None:
        await asyncio.gather(FARM_LOG_TASK, return_exceptions=True)
        FARM_LOG_TASK = None
    FARM_PROCESS = None
    await auth_flow.close_all()
    await manager.manager.close()
    await manager.manager.finish_farm()
    await ai.stop()


# ─── Auth and pages ───
@app.get("/", response_class=HTMLResponse)
async def root(web_auth: Optional[str] = Cookie(default=None)):
    return RedirectResponse("/dashboard" if web_auth else "/login")


@app.get("/register", response_class=HTMLResponse)
async def register_form(request: Request):
    if await db.owner_exists():
        return RedirectResponse("/login")
    return _render(request, "register.html", {})


@app.post("/register")
async def register_post(username: str = Form(...), password: str = Form(...)):
    if await db.owner_exists():
        return RedirectResponse("/login", status_code=303)
    if not username.strip() or len(password) < 10:
        raise HTTPException(400, "Логин обязателен, пароль — минимум 10 символов")
    await db.create_owner(username.strip(), auth.hash_password(password))
    return RedirectResponse("/login", status_code=303)


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    return _render(request, "login.html", {"has_owner": await db.owner_exists()})


@app.post("/login")
async def login_post(username: str = Form(...), password: str = Form(...)):
    owner = await db.get_owner(username.strip())
    if not owner or not auth.verify_password(password, owner["password_hash"]):
        raise HTTPException(401, "Неверный логин или пароль")
    token = auth.make_token(owner["id"], owner["username"])
    response = RedirectResponse("/dashboard", status_code=303)
    response.set_cookie(
        "web_auth", token, httponly=True, samesite="lax", max_age=30 * 24 * 3600,
        secure=os.getenv("WEB_COOKIE_SECURE", "false").lower() == "true",
    )
    return response


@app.get("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("web_auth")
    return response


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, web_auth: Optional[str] = Cookie(default=None)):
    _current_owner(web_auth)
    accounts = [_account_for_ui(row) for row in await db.list_accounts()]
    task_rows = await db.list_tasks(12)
    return _render(request, "dashboard.html", {"accounts": accounts, "tasks": task_rows})


# ─── Account/session management ───
@app.get("/accounts", response_class=HTMLResponse)
async def accounts_page(request: Request, web_auth: Optional[str] = Cookie(default=None)):
    _current_owner(web_auth)
    rows = await db.list_accounts()
    sessions = await db.list_sessions()
    settings = load_farm_settings(await db.get_setting("farm_settings", ""))
    default_api_id, default_api_hash = await manager.manager.default_api_credentials()
    return _render(
        request,
        "accounts.html",
        {
            "accounts": [_account_for_ui(row) for row in rows],
            "sessions": sessions,
            "settings": settings,
            "session_defaults": {
                "api_id": default_api_id or "",
                "api_hash_set": bool(default_api_hash),
            },
            "active_accounts": await manager.manager.list_active(),
        },
    )


@app.post("/api/accounts/scan")
async def api_scan_sessions(web_auth: Optional[str] = Cookie(default=None)):
    _current_owner(web_auth)
    _require_farm_stopped_for_session_check()
    return {"sessions": await manager.manager.scan_sessions_dir()}


@app.post("/api/accounts/session-defaults")
async def api_save_session_defaults(
    api_id: int = Form(...),
    api_hash: str = Form(default=""),
    web_auth: Optional[str] = Cookie(default=None),
):
    _current_owner(web_auth)
    _require_farm_stopped_for_session_check()
    if api_id <= 0:
        raise HTTPException(422, "Telegram API ID должен быть положительным числом")
    supplied_hash = api_hash.strip()
    if supplied_hash:
        resolved_hash = supplied_hash
    else:
        _, resolved_hash = await manager.manager.default_api_credentials()
    if not resolved_hash:
        raise HTTPException(422, "Укажите Telegram API Hash из my.telegram.org")
    await db.set_setting("telegram_api_id", str(api_id))
    await db.set_setting("telegram_api_hash", resolved_hash)
    return {"ok": True, "sessions": await manager.manager.scan_sessions_dir()}


@app.post("/api/accounts/auth/start")
async def api_auth_start(
    name: str = Form(...),
    api_id: Optional[int] = Form(default=None),
    api_hash: str = Form(default=""),
    phone: str = Form(...),
    proxy: str = Form(default=""),
    persona: str = Form(default=""),
    reply_probability: Optional[float] = Form(default=None),
    media_text: Optional[float] = Form(default=None),
    media_gif: Optional[float] = Form(default=None),
    media_sticker: Optional[float] = Form(default=None),
    media_photo: Optional[float] = Form(default=None),
    media_voice: Optional[float] = Form(default=None),
    web_auth: Optional[str] = Cookie(default=None),
):
    _current_owner(web_auth)
    _require_farm_stopped_for_session_check()
    farm_settings = load_farm_settings(await db.get_setting("farm_settings", ""))
    try:
        safe_name = validate_session_name(name)
        existing = await db.get_account(safe_name)
        default_api_id, default_api_hash = await manager.manager.default_api_credentials()
        api_id = api_id or (existing.get("api_id") if existing else None) or default_api_id
        api_hash = api_hash.strip()
        if not api_hash and existing:
            api_hash = str(existing.get("api_hash") or "")
        api_hash = api_hash or default_api_hash
        if not api_id or not api_hash:
            raise ValueError("Введите API ID/Hash или сначала задайте общие credentials вверху страницы")
        if existing:
            persona = persona.strip() or str(existing.get("persona") or "")
            proxy = proxy.strip() or str(existing.get("proxy") or "")
            is_custom = bool(existing.get("behavior_customized", 0))
            probability = float(existing.get("reply_probability") or farm_settings["default_reply_probability"]) if is_custom else float(farm_settings["default_reply_probability"])
            bias = normalize_media_bias(existing.get("media_bias"), farm_settings["default_media_bias"]) if is_custom else farm_settings["default_media_bias"]
        else:
            probability = float(farm_settings["default_reply_probability"])
            bias = farm_settings["default_media_bias"]
            is_custom = False

        explicit_behavior = reply_probability is not None or any(
            value is not None for value in (media_text, media_gif, media_sticker, media_photo, media_voice)
        )
        if explicit_behavior:
            probability = probability if reply_probability is None else _probability(reply_probability, "Вероятность ответа")
            current = bias
            values = [
                media_text if media_text is not None else current["text"] * 100,
                media_gif if media_gif is not None else current["gif"] * 100,
                media_sticker if media_sticker is not None else current["sticker"] * 100,
                media_photo if media_photo is not None else current["photo"] * 100,
                media_voice if media_voice is not None else current["voice"] * 100,
            ]
            bias = _media_weights(*values)
            is_custom = True

        return await auth_flow.start(
            safe_name, api_id, api_hash, phone, proxy, persona, probability, bias, int(is_custom)
        )
    except Exception as exc:
        log.warning("Telegram auth start failed for %s (%s)", name[:64], type(exc).__name__)
        return JSONResponse({"ok": False, "error": _safe_error(exc)}, status_code=400)


@app.post("/api/accounts/auth/code")
async def api_auth_code(
    name: str = Form(...),
    code: str = Form(...),
    web_auth: Optional[str] = Cookie(default=None),
):
    _current_owner(web_auth)
    try:
        return await auth_flow.submit_code(name, code)
    except Exception as exc:
        log.warning("Telegram code verification failed for %s (%s)", name[:64], type(exc).__name__)
        return JSONResponse({"ok": False, "error": _safe_error(exc)}, status_code=400)


@app.post("/api/accounts/auth/password")
async def api_auth_password(
    name: str = Form(...),
    password: str = Form(...),
    web_auth: Optional[str] = Cookie(default=None),
):
    _current_owner(web_auth)
    try:
        return await auth_flow.submit_password(name, password)
    except Exception as exc:
        log.warning("Telegram 2FA verification failed for %s (%s)", name[:64], type(exc).__name__)
        return JSONResponse({"ok": False, "error": _safe_error(exc)}, status_code=400)


@app.post("/api/accounts/security/2fa")
async def api_update_account_2fa(
    name: str = Form(...),
    current_password: str = Form(default=""),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    hint: str = Form(default=""),
    web_auth: Optional[str] = Cookie(default=None),
):
    _current_owner(web_auth)
    try:
        name = validate_session_name(name)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if FARM_PROCESS is not None and FARM_PROCESS.returncode is None:
        raise HTTPException(409, "Остановите чат-ферму перед изменением 2FA аккаунта")
    account = await db.get_account(name)
    if not account or account.get("session_status") != "authorized":
        raise HTTPException(422, "Сначала авторизуйте этот аккаунт в Telegram")
    if len(current_password) > 128:
        raise HTTPException(422, "Текущий пароль 2FA: максимум 128 символов")
    if len(new_password) < 8 or len(new_password) > 128:
        raise HTTPException(422, "Новый пароль 2FA должен содержать от 8 до 128 символов")
    if new_password != confirm_password:
        raise HTTPException(422, "Подтверждение нового пароля не совпадает")
    if current_password and current_password == new_password:
        raise HTTPException(422, "Новый пароль должен отличаться от текущего")
    hint = hint.strip()
    if len(hint) > 128:
        raise HTTPException(422, "Подсказка к паролю: максимум 128 символов")
    if hint and new_password.casefold() in hint.casefold():
        raise HTTPException(422, "Подсказка не должна содержать новый пароль")

    try:
        client = await manager.manager.get_client(name)
        if current_password:
            change_kwargs = {"new_hint": hint} if hint else {}
            changed = await client.change_cloud_password(
                current_password,
                new_password,
                **change_kwargs,
            )
            action = "changed"
        else:
            enable_kwargs = {"hint": hint} if hint else {}
            changed = await client.enable_cloud_password(new_password, **enable_kwargs)
            action = "enabled"
        if changed is False:
            raise RuntimeError("Telegram не подтвердил изменение настроек 2FA")
    except HTTPException:
        raise
    except Exception as exc:
        log.warning("Telegram 2FA settings update failed for %s (%s)", name[:64], type(exc).__name__)
        message = _safe_error(exc)
        if not current_password and "already" in message.casefold():
            message = "Пароль 2FA уже установлен. Введите текущий пароль, чтобы заменить его."
        elif current_password and "no cloud password" in message.casefold():
            message = "Для аккаунта ещё не установлен пароль 2FA. Оставьте поле текущего пароля пустым, чтобы задать новый."
        raise HTTPException(400, message) from exc

    log.info("2FA password %s for account %s; secret was not stored", action, name[:64])
    return {"ok": True, "status": action}


@app.post("/api/accounts/auth/cancel")
async def api_auth_cancel(name: str = Form(...), web_auth: Optional[str] = Cookie(default=None)):
    _current_owner(web_auth)
    try:
        await auth_flow.cancel(name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


@app.post("/api/accounts/upload")
async def api_upload_session(
    session_file: UploadFile = File(...),
    name: str = Form(default=""),
    api_id: int = Form(...),
    api_hash: str = Form(...),
    phone: str = Form(default=""),
    proxy: str = Form(default=""),
    web_auth: Optional[str] = Cookie(default=None),
):
    _current_owner(web_auth)
    _require_farm_stopped_for_session_check()
    original = Path(session_file.filename or "").name
    if not original.endswith(".session"):
        raise HTTPException(400, "Можно загрузить только Pyrogram-файл .session")
    account_name = validate_session_name(name.strip() or Path(original).stem)
    target = SESSIONS_DIR / f"{account_name}.session"
    temporary = SESSIONS_DIR / f".{account_name}-{uuid.uuid4().hex}.upload"
    backup = SESSIONS_DIR / f".{account_name}-{uuid.uuid4().hex}.backup"
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    size = 0
    try:
        with temporary.open("wb") as output:
            while chunk := await session_file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_SESSION_UPLOAD:
                    raise HTTPException(413, "Файл сессии больше 25 МБ")
                output.write(chunk)
        if size == 0:
            raise HTTPException(400, "Файл пустой")
        await manager.manager.close(account_name)
        if target.exists():
            os.replace(target, backup)
        os.replace(temporary, target)
        try:
            info = await import_session_file(f"{account_name}.session", api_id, api_hash, phone, proxy)
        except Exception:
            target.unlink(missing_ok=True)
            if backup.exists():
                os.replace(backup, target)
            raise
        backup.unlink(missing_ok=True)
        return {"ok": True, "info": info}
    except HTTPException:
        raise
    except Exception as exc:
        log.warning("Session upload failed for %s (%s)", account_name, type(exc).__name__)
        return JSONResponse({"ok": False, "error": _safe_error(exc)}, status_code=400)
    finally:
        temporary.unlink(missing_ok=True)
        await session_file.close()


@app.post("/api/accounts/import")
async def api_import_session(
    session_name: str = Form(...),
    api_id: int = Form(...),
    api_hash: str = Form(...),
    phone: str = Form(default=""),
    proxy: str = Form(default=""),
    web_auth: Optional[str] = Cookie(default=None),
):
    _current_owner(web_auth)
    _require_farm_stopped_for_session_check()
    try:
        info = await import_session_file(f"{validate_session_name(session_name)}.session", api_id, api_hash, phone, proxy)
        return {"ok": True, "info": info}
    except FileNotFoundError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
    except Exception as exc:
        log.warning("Session import failed for %s (%s)", session_name[:64], type(exc).__name__)
        return JSONResponse({"ok": False, "error": _safe_error(exc)}, status_code=400)


@app.post("/api/accounts/update")
async def api_update_account(
    name: str = Form(...),
    api_id: Optional[int] = Form(default=None),
    api_hash: str = Form(default=""),
    phone: Optional[str] = Form(default=None),
    proxy: Optional[str] = Form(default=None),
    persona: Optional[str] = Form(default=None),
    reply_probability: Optional[float] = Form(default=None),
    media_text: Optional[float] = Form(default=None),
    media_gif: Optional[float] = Form(default=None),
    media_sticker: Optional[float] = Form(default=None),
    media_photo: Optional[float] = Form(default=None),
    media_voice: Optional[float] = Form(default=None),
    behavior_mode: str = Form(default="global"),
    enabled: int = Form(default=1),
    web_auth: Optional[str] = Cookie(default=None),
):
    _current_owner(web_auth)
    try:
        account_name = validate_session_name(name)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    existing = await db.get_account(account_name)
    if not existing:
        raise HTTPException(404, "Аккаунт не найден")
    mode = behavior_mode.strip().lower()
    if mode not in {"global", "custom"}:
        raise HTTPException(422, "Выберите общие или индивидуальные настройки")
    updates: dict[str, Any] = {
        "enabled": 1 if enabled else 0,
        "behavior_customized": 1 if mode == "custom" else 0,
    }
    credentials_changed = False
    if api_id is not None:
        if api_id <= 0:
            raise HTTPException(422, "API ID должен быть положительным числом")
        updates["api_id"] = api_id
        credentials_changed |= api_id != existing.get("api_id")
    if api_hash.strip():
        updates["api_hash"] = api_hash.strip()
        credentials_changed = True
    if phone is not None:
        updates["phone"] = phone.strip()
    if proxy is not None:
        try:
            if proxy.strip():
                manager.parse_proxy(proxy)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        updates["proxy"] = proxy.strip()
        credentials_changed |= proxy.strip() != (existing.get("proxy") or "")
    if persona is not None:
        updates["persona"] = persona.strip()[:1000]
    if mode == "custom" and reply_probability is not None:
        updates["reply_probability"] = _probability(reply_probability, "Вероятность ответа")
    media_values = (media_text, media_gif, media_sticker, media_photo, media_voice)
    if mode == "custom" and any(value is not None for value in media_values):
        current = normalize_media_bias(existing.get("media_bias"))
        new_values = [
            media_text if media_text is not None else current["text"] * 100,
            media_gif if media_gif is not None else current["gif"] * 100,
            media_sticker if media_sticker is not None else current["sticker"] * 100,
            media_photo if media_photo is not None else current["photo"] * 100,
            media_voice if media_voice is not None else current["voice"] * 100,
        ]
        bias = _media_weights(*new_values)
        updates["media_bias"] = json.dumps(bias, ensure_ascii=False)
    if credentials_changed:
        await manager.manager.close(account_name)
        updates["session_status"] = "unknown"
    await db.upsert_account(account_name, **updates)
    return {"ok": True, "account": _account_for_ui(await db.get_account(account_name))}


@app.post("/api/accounts/set-proxy")
async def api_set_proxy(
    name: str = Form(...), proxy: str = Form(default=""),
    web_auth: Optional[str] = Cookie(default=None),
):
    _current_owner(web_auth)
    try:
        return await manager.manager.update_account_proxy(name, proxy)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/accounts/test-proxy")
async def api_test_proxy(proxy: str = Form(default=""), web_auth: Optional[str] = Cookie(default=None)):
    _current_owner(web_auth)
    return await manager.test_proxy_connection(proxy)


@app.post("/api/accounts/delete")
async def api_delete_account(name: str = Form(...), web_auth: Optional[str] = Cookie(default=None)):
    _current_owner(web_auth)
    try:
        account_name = validate_session_name(name)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await auth_flow.cancel(account_name)
    await manager.manager.close(account_name)
    await db.delete_account(account_name)
    # Keep the local .session file unless the operator removes it explicitly.
    return {"ok": True, "session_file_kept": (SESSIONS_DIR / f"{account_name}.session").exists()}


# ─── Chat activity ───
@app.get("/chatfarm", response_class=HTMLResponse)
async def chatfarm_page(request: Request, web_auth: Optional[str] = Cookie(default=None)):
    _current_owner(web_auth)
    accounts = [_account_for_ui(row) for row in await db.list_accounts()]
    settings = load_farm_settings(await db.get_setting("farm_settings", ""))
    targets = await db.list_targets()
    try:
        target_prefill = str(int(request.query_params.get("target_id", "")))
    except (TypeError, ValueError):
        target_prefill = ""
    try:
        topic_prefill = str(max(0, int(request.query_params.get("topic_id", "0"))))
    except (TypeError, ValueError):
        topic_prefill = "0"
    history_source_prefill = request.query_params.get("history_source", "")[:512]
    try:
        history_topic_prefill = str(max(0, int(request.query_params.get("history_topic_id", "0"))))
    except (TypeError, ValueError):
        history_topic_prefill = "0"
    return _render(
        request,
        "chatfarm.html",
        {
            "accounts": accounts,
            "settings": settings,
            "targets": targets,
            "target_prefill": target_prefill,
            "topic_prefill": topic_prefill,
            "history_source_prefill": history_source_prefill,
            "history_topic_prefill": history_topic_prefill,
        },
    )


@app.get("/context", response_class=HTMLResponse)
async def chat_context_page(request: Request, web_auth: Optional[str] = Cookie(default=None)):
    _current_owner(web_auth)
    rows = await db.list_accounts(enabled_only=True)
    accounts = [
        _account_for_ui(row) for row in rows
        if row.get("session_status") == "authorized" and row.get("api_id") and row.get("api_hash")
    ]
    return _render(
        request,
        "context.html",
        {"accounts": accounts, "contexts": chat_context.list_context_summaries()},
    )


@app.post("/api/chat-context/collect")
async def api_collect_chat_context(
    chat_link: str = Form(...),
    reader: str = Form(...),
    accounts: str = Form(...),
    history_limit: int = Form(default=100),
    topic_id: int = Form(default=0),
    download_media: Optional[str] = Form(default=None),
    auto_join: Optional[str] = Form(default=None),
    authorization_ack: Optional[str] = Form(default=None),
    web_auth: Optional[str] = Cookie(default=None),
):
    _current_owner(web_auth)
    _require_farm_stopped_for_session_check()
    if authorization_ack is None:
        raise HTTPException(422, "Подтвердите разрешение и объявление участникам об автоматическом сборе истории")
    try:
        reference = chat_context.parse_chat_link(chat_link)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    auto_join_enabled = auto_join is not None
    if auto_join_enabled and reference.get("source") not in {"invite", "username"}:
        raise HTTPException(
            422,
            "Для авто-вступления используйте публичный username или invite-ссылку; одного ID или t.me/c недостаточно.",
        )
    if history_limit < 0:
        raise HTTPException(422, "Глубина истории должна быть неотрицательной; 0 означает всю доступную историю")
    if topic_id < 0:
        raise HTTPException(422, "ID темы должен быть положительным числом")
    try:
        reader_name = validate_session_name(reader)
        account_names = list(dict.fromkeys(
            validate_session_name(name.strip()) for name in accounts.split(",") if name.strip()
        ))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not account_names or reader_name not in account_names:
        raise HTTPException(422, "Выберите аккаунты и включите сессию для чтения истории в список")
    for name in account_names:
        account = await db.get_account(name)
        if (
            not account or not account.get("enabled") or not account.get("api_id")
            or not account.get("api_hash") or account.get("session_status") != "authorized"
        ):
            raise HTTPException(422, f"Аккаунт {name} выключен, не настроен или не авторизован")
    invite_token = _store_invite_reference(reference) if reference.get("invite_hash") else None
    payload = {
        "reference": {
            "chat_ref": reference["chat_ref"],
            "invite_hash": None,
            "topic_id": reference.get("topic_id"),
            "source": reference.get("source"),
        },
        "reader": reader_name,
        "accounts": account_names,
        "history_limit": history_limit,
        "topic_id": topic_id,
        "download_media": download_media is not None,
        "auto_join": auto_join_enabled,
    }
    if invite_token:
        payload["invite_token"] = invite_token
    try:
        task_id = await tasks.runner.submit("chat_context_collect", payload)
    except Exception:
        _discard_invite_reference(invite_token)
        raise
    return {"ok": True, "task_id": task_id}


@app.get("/api/chat-context/list")
async def api_chat_context_list(web_auth: Optional[str] = Cookie(default=None)):
    _current_owner(web_auth)
    return {"contexts": chat_context.list_context_summaries()}


@app.post("/api/chat-context/delete")
async def api_delete_chat_context(
    chat_id: int = Form(...),
    web_auth: Optional[str] = Cookie(default=None),
):
    _current_owner(web_auth)
    if FARM_PROCESS is not None and FARM_PROCESS.returncode is None:
        raise HTTPException(409, "Сначала остановите чат-ферму, чтобы контекст и история не восстановились из памяти процесса")
    try:
        deleted = chat_context.delete_chat_context(chat_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"ok": True, "deleted": deleted}


@app.post("/api/chatfarm/start")
async def api_chatfarm_start(
    accounts: str = Form(...),
    target_id: int = Form(...),
    topic_id: int = Form(default=0),
    min_delay: float = Form(default=2),
    max_delay: float = Form(default=8),
    qa_probability: float = Form(default=0.25),
    clone_probability: float = Form(default=0.25),
    reaction_probability: float = Form(default=0.35),
    scenario_mode: str = Form(default="reactive"),
    scenario_topic: str = Form(default=""),
    collect_context_history: Optional[str] = Form(default=None),
    context_reader: str = Form(default=""),
    history_limit: int = Form(default=100),
    history_source: str = Form(default=""),
    history_topic_id: int = Form(default=0),
    auto_join_history: Optional[str] = Form(default=None),
    scenario_turns: int = Form(default=20),
    joke_every: int = Form(default=5),
    rest_every: int = Form(default=6),
    rest_min_sec: int = Form(default=60),
    rest_max_sec: int = Form(default=120),
    roulette_numbers: str = Form(default="0-36"),
    post_opening: Optional[str] = Form(default=None),
    automation_ack: Optional[str] = Form(default=None),
    web_auth: Optional[str] = Cookie(default=None),
):
    _current_owner(web_auth)
    scenario_mode = scenario_mode.strip().lower()
    if scenario_mode not in {"reactive", "discussion", "roulette", "combined", "history_dialogue"}:
        raise HTTPException(422, "Выберите доступный режим чата")
    if automation_ack is None:
        raise HTTPException(
            422,
            "Подтвердите разрешение, объявление участникам об автоматизации, членство в целевом чате и (если выбрано) вступление в источник истории",
        )
    collect_history = (
        scenario_mode != "reactive"
        and (collect_context_history is not None or scenario_mode == "history_dialogue")
    )
    if collect_history and scenario_mode not in {"discussion", "combined", "history_dialogue"}:
        raise HTTPException(422, "История чата доступна только для режимов диалога")
    if collect_history and history_limit < 0:
        raise HTTPException(422, "Глубина истории должна быть неотрицательной; 0 означает всю доступную историю")
    behavior_only = scenario_mode == "reactive"
    topicless_history_dialogue = scenario_mode == "history_dialogue"
    scenario_topic = "" if behavior_only or topicless_history_dialogue else scenario_topic.strip()
    if not scenario_topic and collect_history and not topicless_history_dialogue:
        scenario_topic = "Прозрачный сценарный диалог по общим идеям из недавней истории чата; без имитации участников."
    if len(scenario_topic) > 2000:
        raise HTTPException(422, "Тема или правила сценария: максимум 2000 символов")
    names = list(dict.fromkeys(name.strip() for name in accounts.split(",") if name.strip()))
    if not names:
        raise HTTPException(422, "Выберите хотя бы один активный аккаунт")
    if target_id == 0:
        raise HTTPException(422, "Укажите Chat ID")
    if topic_id < 0:
        raise HTTPException(422, "ID темы должен быть положительным числом")
    try:
        clean_names = [validate_session_name(name) for name in names]
        history_reader = validate_session_name(context_reader) if collect_history else ""
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if collect_history and (not history_reader or history_reader not in clean_names):
        raise HTTPException(422, "Выберите сессию для чтения истории среди аккаунтов диалога")
    history_source_reference = None
    effective_history_topic = 0
    auto_join_history_enabled = collect_history and auto_join_history is not None
    if collect_history:
        source_value = history_source.strip()
        if topicless_history_dialogue and not source_value:
            raise HTTPException(422, "Укажите ID или ссылку чата-источника истории отдельно от целевого чата")
        try:
            history_source_reference = (
                chat_context.parse_chat_link(source_value)
                if source_value
                else {
                    "chat_ref": target_id,
                    "invite_hash": None,
                    "topic_id": None,
                    "source": "id",
                }
            )
        except ValueError as exc:
            raise HTTPException(422, f"Некорректный чат-источник истории: {exc}") from exc
        if history_topic_id < 0:
            raise HTTPException(422, "ID темы источника должен быть положительным числом")
        effective_history_topic = (
            history_topic_id
            or history_source_reference.get("topic_id")
            or (topic_id if not source_value else 0)
        )
        if auto_join_history_enabled and history_source_reference.get("source") not in {"invite", "username"}:
            raise HTTPException(
                422,
                "Для авто-вступления в источник укажите публичный username или invite-ссылку; одного числового ID/t.me/c недостаточно.",
            )
    for name in clean_names:
        account = await db.get_account(name)
        if (
            not account
            or not account.get("enabled")
            or not account.get("api_id")
            or not account.get("api_hash")
            or account.get("session_status") != "authorized"
        ):
            raise HTTPException(422, f"Аккаунт {name} выключен, не настроен или не авторизован")
    try:
        min_delay = float(min_delay)
        max_delay = float(max_delay)
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, "Задержка должна быть числом") from exc
    if not math.isfinite(min_delay) or not math.isfinite(max_delay) or min_delay < 0 or max_delay < min_delay or max_delay > 86400:
        raise HTTPException(422, "Задайте корректный диапазон задержки (0–86400 секунд)")
    if scenario_mode != "reactive":
        if len(clean_names) < 2:
            raise HTTPException(422, "Для сценария с диалогом выберите минимум два аккаунта")
        if not scenario_topic and not topicless_history_dialogue:
            raise HTTPException(422, "Укажите общую тему или правила сценария")
        if min_delay < 5:
            raise HTTPException(422, "Для диалога между аккаунтами пауза должна быть не короче 5 секунд")
        if not 0 <= scenario_turns <= 500:
            raise HTTPException(422, "Количество ходов должно быть 0–500 (0 — до ручной остановки)")
        if not 0 <= joke_every <= 1000 or not 0 <= rest_every <= 1000:
            raise HTTPException(422, "Частота анекдотов и отдыха должна быть в диапазоне 0–1000")
        if rest_every and (
            rest_min_sec < 15 or rest_max_sec < rest_min_sec or rest_max_sec > 86400
        ):
            raise HTTPException(422, "Задайте паузу отдыха от 15 секунд до 24 часов")
        if scenario_mode == "roulette":
            try:
                parse_roulette_numbers(roulette_numbers)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
    if FARM_PROCESS is not None and FARM_PROCESS.returncode is None:
        raise HTTPException(409, "Чат-ферма уже запущена")
    settings = {
        "accounts": clean_names,
        "target_id": target_id,
        "topic_id": topic_id,
        "min_delay": min_delay,
        "max_delay": max_delay,
        "qa_probability": _probability(qa_probability, "Вероятность Q&A"),
        "clone_probability": _probability(clone_probability, "Вероятность диалогов"),
        "reaction_probability": _probability(reaction_probability, "Вероятность реакции"),
        "scenario_mode": scenario_mode,
        "scenario_topic": scenario_topic,
        "collect_history": collect_history,
        "context_reader": history_reader,
        "history_limit": history_limit if collect_history else 0,
        "history_reference": ({
            "chat_ref": history_source_reference["chat_ref"],
            "invite_hash": None,
            "topic_id": history_source_reference.get("topic_id"),
            "source": history_source_reference.get("source"),
        } if collect_history and history_source_reference else None),
        "history_topic_id": effective_history_topic,
        "history_auto_join": auto_join_history_enabled,
        "scenario_turns": 20 if behavior_only else scenario_turns,
        "joke_every": 0 if behavior_only else joke_every,
        "rest_every": 0 if behavior_only else rest_every,
        "rest_min_sec": 60 if behavior_only else rest_min_sec,
        "rest_max_sec": 120 if behavior_only else rest_max_sec,
        "roulette_numbers": "0-36" if behavior_only else roulette_numbers.strip(),
        "post_opening": not behavior_only and not topicless_history_dialogue and post_opening is not None,
        "automation_acknowledged": automation_ack is not None,
    }
    history_invite_token = (
        _store_invite_reference(history_source_reference)
        if collect_history and history_source_reference and history_source_reference.get("invite_hash")
        else None
    )
    if history_invite_token:
        settings["history_invite_token"] = history_invite_token
    try:
        task_id = await tasks.runner.submit("start_chatfarm", settings)
    except Exception:
        _discard_invite_reference(history_invite_token)
        raise
    return {"ok": True, "task_id": task_id, "collecting_history": collect_history}


async def _stream_farm_logs(process: asyncio.subprocess.Process) -> None:
    global FARM_PROCESS, FARM_LOG_TASK
    try:
        if process.stdout:
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").rstrip()
                with FARM_LOG_FILE.open("a", encoding="utf-8", errors="replace") as logfile:
                    logfile.write(text + "\n")
                log.info("[farm.py] %s", text)
        await process.wait()
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Ошибка чтения лога фермы")
    finally:
        if FARM_PROCESS is process:
            FARM_PROCESS = None
        if FARM_LOG_TASK is asyncio.current_task():
            FARM_LOG_TASK = None
        await manager.manager.finish_farm()


@tasks.register("start_chatfarm")
async def _h_start_chatfarm(payload: dict) -> dict:
    """Launch farm.py and stream its output without blocking the task queue."""
    global FARM_PROCESS, FARM_LOG_TASK
    if FARM_PROCESS is not None and FARM_PROCESS.returncode is None:
        raise RuntimeError("Чат-ферма уже работает")
    if manager.manager.farm_sessions_busy():
        raise RuntimeError("Обнаружена работающая чат-ферма; сначала остановите её")
    history_result = None
    if payload.get("collect_history"):
        history_reference = payload.get("history_reference") or {
            "chat_ref": int(payload["target_id"]),
            "invite_hash": None,
            "topic_id": int(payload.get("topic_id") or 0) or None,
            "source": "id",
        }
        invite_token = payload.pop("history_invite_token", None)
        if invite_token:
            resolved_reference = _take_invite_reference(invite_token)
            if resolved_reference is None:
                raise RuntimeError("Invite-ссылка больше недоступна в памяти. Повторите запуск фермы.")
            history_reference = resolved_reference
        history_result = await chat_context.collect_chat_context({
            "reference": history_reference,
            "reader": payload.get("context_reader"),
            "accounts": payload.get("accounts", []),
            "history_limit": int(
                payload.get("history_limit")
                if payload.get("history_limit") is not None
                else chat_context.DEFAULT_HISTORY_LIMIT
            ),
            "topic_id": int(
                payload.get("history_topic_id")
                if payload.get("history_topic_id") is not None
                else payload.get("topic_id") or 0
            ),
            "download_media": False,
            "auto_join": bool(payload.get("history_auto_join", False)),
        })
        if payload.get("scenario_mode") == "history_dialogue":
            account_names = list(dict.fromkeys(payload.get("accounts", [])))
            participant_ids = set()
            for name, participant_id in (history_result.get("account_participant_ids") or {}).items():
                if name in account_names:
                    try:
                        participant_ids.add(int(participant_id))
                    except (TypeError, ValueError):
                        continue
            if len(participant_ids) < len(account_names):
                raise RuntimeError(
                    f"Выбрано аккаунтов: {len(account_names)}, но в этой глубине истории найдено "
                    f"только {len(participant_ids)} разных участников с контекстом. "
                    "Увеличьте глубину (0 — вся доступная история), выберите другой источник "
                    "или сократите число аккаунтов. Для 30 аккаунтов нужны 30 разных участников."
                )
        assignments = ", ".join(
            f"{name} → участник {participant_id}"
            for name, participant_id in (history_result.get("account_participant_ids") or {}).items()
        ) or "пар для участников нет"
        log.info(
            "Собран обезличенный контекст чата %s: %d сообщений; назначение истории: %s; теперь запускаю чат-ферму",
            history_result["chat_id"],
            history_result["message_count"],
            assignments,
        )
    env = os.environ.copy()
    env.update({
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
        "FARM_OVERRIDE_TARGET": str(payload["target_id"]),
        "FARM_OVERRIDE_CONTEXT_CHAT_ID": str(
            history_result["chat_id"] if history_result else payload["target_id"]
        ),
        "FARM_OVERRIDE_CONTEXT_REFRESH": "1" if history_result else "0",
        "FARM_OVERRIDE_TOPIC": str(payload.get("topic_id", 0)),
        "FARM_OVERRIDE_ACCOUNTS": ",".join(payload["accounts"]),
        "FARM_OVERRIDE_MIN_DELAY": str(payload["min_delay"]),
        "FARM_OVERRIDE_MAX_DELAY": str(payload["max_delay"]),
        "FARM_OVERRIDE_QA_PROBABILITY": str(payload["qa_probability"]),
        "FARM_OVERRIDE_CLONE_PROBABILITY": str(payload["clone_probability"]),
        "FARM_OVERRIDE_REACTION_PROBABILITY": str(payload["reaction_probability"]),
        "FARM_OVERRIDE_SCENARIO_MODE": str(payload.get("scenario_mode", "reactive")),
        "FARM_OVERRIDE_SCENARIO_TOPIC": str(payload.get("scenario_topic", "")),
        "FARM_OVERRIDE_SCENARIO_TURNS": str(payload.get("scenario_turns", 20)),
        "FARM_OVERRIDE_JOKE_EVERY": str(payload.get("joke_every", 5)),
        "FARM_OVERRIDE_REST_EVERY": str(payload.get("rest_every", 6)),
        "FARM_OVERRIDE_REST_MIN": str(payload.get("rest_min_sec", 60)),
        "FARM_OVERRIDE_REST_MAX": str(payload.get("rest_max_sec", 120)),
        "FARM_OVERRIDE_ROULETTE_NUMBERS": str(payload.get("roulette_numbers", "0-36")),
        "FARM_OVERRIDE_POST_OPENING": "1" if payload.get("post_opening", True) else "0",
    })
    await manager.manager.prepare_for_farm()
    try:
        FARM_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        FARM_LOG_FILE.write_text("", encoding="utf-8")
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-u",
            str(db.ROOT / "farm.py"),
            "--debug",
            cwd=str(db.ROOT),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except BaseException:
        await manager.manager.finish_farm()
        raise
    FARM_PROCESS = process
    FARM_LOG_TASK = asyncio.create_task(_stream_farm_logs(process), name="farm-log-reader")
    log.info("farm.py запущен (PID=%s)", process.pid)
    return {"pid": process.pid}


@app.get("/api/chatfarm/status")
async def api_chatfarm_status(web_auth: Optional[str] = Cookie(default=None)):
    _current_owner(web_auth)
    running = FARM_PROCESS is not None and FARM_PROCESS.returncode is None
    return {"running": running, "pid": FARM_PROCESS.pid if running else None}


@app.get("/api/chatfarm/logs")
async def api_chatfarm_logs(web_auth: Optional[str] = Cookie(default=None)):
    _current_owner(web_auth)
    if not FARM_LOG_FILE.exists():
        return {"logs": ["Лог пока пуст. Запустите чат-ферму."]}
    try:
        with FARM_LOG_FILE.open("rb") as log_file:
            log_file.seek(0, os.SEEK_END)
            size = log_file.tell()
            log_file.seek(max(0, size - 128 * 1024))
            text = log_file.read().decode("utf-8", "replace")
        lines = text.splitlines()
        return {"logs": lines[-120:] if lines else ["Ожидание вывода..."]}
    except OSError as exc:
        return {"logs": [f"Ошибка чтения лога: {exc}"]}


@app.post("/api/chatfarm/stop")
async def api_chatfarm_stop(web_auth: Optional[str] = Cookie(default=None)):
    _current_owner(web_auth)
    global FARM_PROCESS
    process = FARM_PROCESS
    stopped = False
    if process is not None and process.returncode is None:
        try:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=8.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
            stopped = True
        except ProcessLookupError:
            stopped = True
        except Exception:
            log.exception("Ошибка остановки farm.py")
        finally:
            if FARM_PROCESS is process:
                FARM_PROCESS = None
    if stopped:
        await manager.manager.finish_farm()
    return {"ok": True, "stopped": stopped}


# ─── Comments ───
@app.get("/comments", response_class=HTMLResponse)
async def comments_page(request: Request, web_auth: Optional[str] = Cookie(default=None)):
    _current_owner(web_auth)
    accounts = [_account_for_ui(row) for row in await db.list_accounts(enabled_only=True)]
    return _render(request, "comments.html", {"accounts": accounts})


@app.post("/api/comments/generate")
async def api_generate_comment(
    url: str = Form(...), topic: str = Form(default=""),
    web_auth: Optional[str] = Cookie(default=None),
):
    _current_owner(web_auth)
    text = await ai.ask(
        "Напиши короткий естественный комментарий к публикации: "
        f"{url}\nКонтекст/тон: {topic or 'нейтральный'}. Не выдумывай факты и не выдавай себя за автора.",
        new_conversation=True,
    )
    return {"text": text}


@app.post("/api/comments/post")
async def api_post_comment(
    account: str = Form(...), url: str = Form(...), comment: str = Form(...),
    web_auth: Optional[str] = Cookie(default=None),
):
    _current_owner(web_auth)
    task_id = await tasks.runner.submit("comment_post", {"account": account, "url": url, "comment": comment})
    return {"ok": True, "task_id": task_id}


# ─── Account actions ───
@app.get("/mass", response_class=HTMLResponse)
async def mass_page(request: Request, web_auth: Optional[str] = Cookie(default=None)):
    _current_owner(web_auth)
    accounts = [_account_for_ui(row) for row in await db.list_accounts(enabled_only=True)]
    return _render(request, "mass.html", {"accounts": accounts})


@app.post("/api/mass/join")
async def api_mass_join(
    accounts: str = Form(...), target: str = Form(...),
    min_delay: float = Form(default=5), max_delay: float = Form(default=20),
    web_auth: Optional[str] = Cookie(default=None),
):
    _current_owner(web_auth)
    names = [name.strip() for name in accounts.split(",") if name.strip()]
    if not names:
        raise HTTPException(422, "Выберите хотя бы один аккаунт")
    if min_delay < 0 or max_delay < min_delay:
        raise HTTPException(422, "Некорректная задержка")
    task_id = await tasks.runner.submit(
        "mass_join", {"accounts": names, "target": target.strip(), "delay": [min_delay, max_delay]}
    )
    return {"ok": True, "task_id": task_id}


@app.post("/api/mass/bio")
async def api_mass_bio(
    accounts: str = Form(...), bio: str = Form(...),
    web_auth: Optional[str] = Cookie(default=None),
):
    _current_owner(web_auth)
    names = [name.strip() for name in accounts.split(",") if name.strip()]
    task_id = await tasks.runner.submit("mass_bio", {"accounts": names, "bio": bio})
    return {"ok": True, "task_id": task_id}


@app.post("/api/mass/create-group")
async def api_create_group(
    account: str = Form(...), title: str = Form(...),
    web_auth: Optional[str] = Cookie(default=None),
):
    _current_owner(web_auth)
    task_id = await tasks.runner.submit("create_group", {"account": account, "title": title})
    return {"ok": True, "task_id": task_id}


# ─── Parser ───
@app.get("/parser", response_class=HTMLResponse)
async def parser_page(request: Request, web_auth: Optional[str] = Cookie(default=None)):
    _current_owner(web_auth)
    accounts = [_account_for_ui(row) for row in await db.list_accounts(enabled_only=True)]
    return _render(request, "parser.html", {"accounts": accounts})


@app.post("/api/parser/users")
async def api_parse_users(
    reader: str = Form(...), chat_id: int = Form(...), limit: int = Form(default=1000),
    web_auth: Optional[str] = Cookie(default=None),
):
    _current_owner(web_auth)
    limit = max(1, min(int(limit), 10000))
    task_id = await tasks.runner.submit("parse_users", {"reader": reader, "chat_id": chat_id, "limit": limit})
    return {"ok": True, "task_id": task_id}


# ─── GIF provider / settings ───
@app.get("/api/gif/search")
async def api_gif_search(q: str, web_auth: Optional[str] = Cookie(default=None)):
    _current_owner(web_auth)
    query = (q or "").strip()
    if not query:
        raise HTTPException(422, "Введите тему для поиска GIF")
    urls = await giphy.search_gif(query, 20)
    return {"urls": urls}


@app.get("/api/photo/random")
async def api_photo_random(web_auth: Optional[str] = Cookie(default=None)):
    _current_owner(web_auth)
    return {"url": await giphy.random_photo()}


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, web_auth: Optional[str] = Cookie(default=None)):
    _current_owner(web_auth)
    raw = await db.get_setting("farm_settings", "")
    settings = load_farm_settings(raw)
    keys = {
        "giphy_key_set": bool(os.getenv("GIPHY_KEY") or await db.get_setting("giphy_key", "")),
        "tenor_key_set": bool(os.getenv("TENOR_KEY") or await db.get_setting("tenor_key", "")),
    }
    return _render(
        request,
        "settings.html",
        {"settings": settings, "keys": keys,
         "deepseek_available": ai.available, "deepseek_connected": ai.connected},
    )


@app.get("/api/settings")
async def api_settings_get(web_auth: Optional[str] = Cookie(default=None)):
    _current_owner(web_auth)
    settings = load_farm_settings(await db.get_setting("farm_settings", ""))
    return {
        "farm": settings,
        "giphy_key_set": bool(os.getenv("GIPHY_KEY") or await db.get_setting("giphy_key", "")),
        "tenor_key_set": bool(os.getenv("TENOR_KEY") or await db.get_setting("tenor_key", "")),
        "deepseek_bridge_available": ai.available,
        "deepseek_connected": ai.connected,
    }


@app.post("/api/settings/save")
async def api_settings_save(
    agent_prompt: str = Form(default=DEFAULT_FARM_SETTINGS["agent_prompt"]),
    min_delay_sec: float = Form(default=2),
    max_delay_sec: float = Form(default=8),
    default_reply_probability: float = Form(default=0.85),
    reaction_probability: float = Form(default=0.35),
    qa_probability: float = Form(default=0.25),
    clone_probability: float = Form(default=0.25),
    proactive_enabled: Optional[str] = Form(default=None),
    typing_simulation: Optional[str] = Form(default=None),
    deepseek_model: str = Form(default="default"),
    deepseek_thinking: Optional[str] = Form(default=None),
    deepseek_search: Optional[str] = Form(default=None),
    media_text: float = Form(default=60),
    media_gif: float = Form(default=12),
    media_sticker: float = Form(default=12),
    media_photo: float = Form(default=8),
    media_voice: float = Form(default=8),
    giphy_key: str = Form(default=""),
    tenor_key: str = Form(default=""),
    clear_giphy_key: Optional[str] = Form(default=None),
    clear_tenor_key: Optional[str] = Form(default=None),
    web_auth: Optional[str] = Cookie(default=None),
):
    _current_owner(web_auth)
    if min_delay_sec < 0 or max_delay_sec < min_delay_sec or max_delay_sec > 86400:
        raise HTTPException(422, "Задайте корректный интервал задержки")
    if len(agent_prompt.strip()) > 5000:
        raise HTTPException(422, "Общие указания агента: максимум 5000 символов")
    model = deepseek_model.strip().lower()
    if model not in {"default", "expert"}:
        raise HTTPException(422, "Режим DeepSeek должен быть default или expert")
    media_bias = _media_weights(media_text, media_gif, media_sticker, media_photo, media_voice)
    saved = load_farm_settings({
        "agent_prompt": agent_prompt.strip(),
        "min_delay_sec": min_delay_sec,
        "max_delay_sec": max_delay_sec,
        "default_reply_probability": _probability(default_reply_probability, "Вероятность ответа"),
        "reaction_probability": _probability(reaction_probability, "Вероятность реакции"),
        "qa_probability": _probability(qa_probability, "Вероятность Q&A"),
        "clone_probability": _probability(clone_probability, "Вероятность диалогов"),
        "proactive_enabled": proactive_enabled is not None,
        "typing_simulation": typing_simulation is not None,
        "deepseek_model": model,
        "deepseek_thinking": deepseek_thinking is not None,
        "deepseek_search": deepseek_search is not None,
        "default_media_bias": media_bias,
    })
    await db.set_setting("farm_settings", json.dumps(saved, ensure_ascii=False))
    if clear_giphy_key is not None:
        await db.set_setting("giphy_key", "")
    elif giphy_key.strip():
        await db.set_setting("giphy_key", giphy_key.strip())
    if clear_tenor_key is not None:
        await db.set_setting("tenor_key", "")
    elif tenor_key.strip():
        await db.set_setting("tenor_key", tenor_key.strip())
    return {
        "ok": True,
        "settings": saved,
        "giphy_key_set": bool(os.getenv("GIPHY_KEY") or await db.get_setting("giphy_key", "")),
        "tenor_key_set": bool(os.getenv("TENOR_KEY") or await db.get_setting("tenor_key", "")),
    }


@app.get("/tasks", response_class=HTMLResponse)
async def tasks_page(request: Request, web_auth: Optional[str] = Cookie(default=None)):
    _current_owner(web_auth)
    items = await db.list_tasks(100)
    return _render(request, "tasks.html", {"tasks": items})


@app.get("/api/tasks/list")
async def api_tasks_list(web_auth: Optional[str] = Cookie(default=None)):
    _current_owner(web_auth)
    return {"tasks": await db.list_tasks(100)}
