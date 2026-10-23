"""FastAPI web control panel on port 1111 (`airdrop_web.py`).

Provides:
- Real-time monitoring & 1-click control of all 4 services (:1111, :8080, agent, bot)
- Wallet manager with 1-click creation, multi-chain balance check, and Exodus / Rabby
  12-word recovery phrase + private key export
- Project & income forecast calculator + 5 earning streams beyond just airdrops
- Beginner step-by-step guide ("Что делать с нуля") and interactive daily checklist
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import stat
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import chains, db, eligibility, forecast, supervisor, wallet
from .agent import discovery_pass, eligibility_pass
from .models import Airdrop
from .settings import CHAIN_IDS, ROOT, settings

log = logging.getLogger("airdrop.panel")
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

app = FastAPI(title="0x4 · Центр управления и заработка (:1111)")


def ensure_panel_passphrase() -> str:
    """Guarantee AIRDROP_KEY_PASSPHRASE is available so wallet actions never fail."""
    settings.ensure_dirs()
    current = os.getenv("AIRDROP_KEY_PASSPHRASE", "").strip()
    if len(current) >= wallet.MIN_PASSPHRASE:
        return current

    pass_file = settings.data_dir / "panel.passphrase"
    if pass_file.exists():
        stored = pass_file.read_text(encoding="utf-8").strip()
        if len(stored) >= wallet.MIN_PASSPHRASE:
            os.environ["AIRDROP_KEY_PASSPHRASE"] = stored
            return stored

    generated = secrets.token_urlsafe(24)
    pass_file.write_text(generated, encoding="utf-8")
    try:
        os.chmod(pass_file, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    os.environ["AIRDROP_KEY_PASSPHRASE"] = generated

    # Persist to .env if present and not already set there
    env_path = ROOT / ".env"
    try:
        if env_path.exists():
            text = env_path.read_text(encoding="utf-8")
            if "AIRDROP_KEY_PASSPHRASE=" not in text:
                with env_path.open("a", encoding="utf-8") as fh:
                    fh.write(f"\nAIRDROP_KEY_PASSPHRASE={generated}\n")
    except OSError:
        pass
    return generated


def _update_env_vars(updates: Dict[str, str]) -> None:
    """Save key=value pairs into .env and current process environment."""
    for k, v in updates.items():
        os.environ[k] = v
    env_path = ROOT / ".env"
    lines: List[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    seen = set()
    new_lines: List[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                new_lines.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        new_lines.append(line)
    for k, v in updates.items():
        if k not in seen:
            new_lines.append(f"{k}={v}")
    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    try:
        os.chmod(env_path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


@app.on_event("startup")
async def _on_startup() -> None:
    ensure_panel_passphrase()
    await db.init_db()
    # Purge records from older demo/offline builds so fictional fixture URLs never
    # appear in the live operator's panel. Offline fixtures remain for tests only.
    await db.remove_demo_projects()
    # Do not seed the live database with fictional projects or test fixtures.
    # Real findings are added only by the online discovery sources or operator config.
    # Ensure at least one starter wallet exists so the user immediately has a wallet for Exodus
    if not wallet.list_wallets():
        try:
            wallet.create_wallet("main")
            await forecast.toggle_checklist_item("step_wallet", True)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not auto-create starter wallet: %s", exc)
    # Seed initial discovery if DB only has starter items
    stats = await db.stats()
    if stats.get("projects", 0) <= len(forecast.STARTER_OPPORTUNITIES):
        try:
            await discovery_pass(offline=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("initial offline discovery failed: %s", exc)
    # Start sibling services (:8080 panel, agent loop, bot if configured)
    supervisor.ensure_sibling_services()


def _tail_log(path: Path, lines: int = 30) -> str:
    if not path.exists():
        return "Лог пока пуст — нажмите «Запустить поиск сейчас», чтобы увидеть записи."
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(content[-lines:]) or "Лог пуст."
    except OSError as exc:
        return f"Не удалось прочитать лог: {exc}"


def _enrich_wallets(include_secrets_for: Optional[str] = None) -> List[Dict[str, Any]]:
    ensure_panel_passphrase()
    items = wallet.list_wallets()
    out: List[Dict[str, Any]] = []
    for entry in items:
        name = entry["name"]
        address = entry.get("address") or ""
        if not address:
            try:
                address = wallet.wallet_address(name)
            except Exception:
                address = ""
        row: Dict[str, Any] = {
            **entry,
            "address": address,
            "debank_url": f"https://debank.com/profile/{address}" if address else "",
            "basescan_url": chains.address_url("base", address) if address else "",
            "arbiscan_url": chains.address_url("arbitrum", address) if address else "",
            "etherscan_url": chains.address_url("ethereum", address) if address else "",
            "secrets": None,
        }
        if include_secrets_for and include_secrets_for == name:
            try:
                sec = wallet.export_wallet_secrets(name)
                words = [w for w in (sec.get("mnemonic") or "").split() if w]
                row["secrets"] = {
                    **sec,
                    "words": list(enumerate(words, start=1)),
                }
            except Exception as exc:  # noqa: BLE001
                row["secrets_error"] = str(exc)
        out.append(row)
    return out


# --------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
async def dashboard_page(
    request: Request,
    msg: str = Query(default=""),
    err: str = Query(default=""),
) -> HTMLResponse:
    ensure_panel_passphrase()
    services = await supervisor.services_status()
    stats = await db.stats()
    wallets = _enrich_wallets()
    checklist = await forecast.get_checklist_state()
    proj_forecast = await forecast.build_project_forecast(hours_per_day=1.0, budget_usd=0.0)
    top_drops = await db.list_projects(limit=8)
    actions_log = await db.list_actions(limit=8)
    paused = await db.get_kv("paused", "0") == "1"

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "page": "dashboard",
            "msg": msg,
            "err": err,
            "services": services,
            "stats": stats,
            "wallets": wallets,
            "checklist": checklist,
            "forecast": proj_forecast,
            "top_drops": top_drops,
            "actions_log": actions_log,
            "paused": paused,
            "dry_run": settings.dry_run,
            "bot_token_set": bool(os.getenv("AIRDROP_BOT_TOKEN", "").strip()),
            "bot_admin_ids": os.getenv("AIRDROP_ADMIN_IDS", "") or os.getenv("ADMIN_IDS", ""),
            "agent_log": _tail_log(settings.log_path, 25),
        },
    )


@app.get("/wallets", response_class=HTMLResponse)
async def wallets_page(
    request: Request,
    reveal: str = Query(default=""),
    check_balances: int = Query(default=0),
    msg: str = Query(default=""),
    err: str = Query(default=""),
) -> HTMLResponse:
    ensure_panel_passphrase()
    wallets = _enrich_wallets(include_secrets_for=reveal.strip() or None)
    if reveal.strip():
        await forecast.toggle_checklist_item("step_exodus", True)

    balances: Dict[str, Dict[str, Any]] = {}
    if check_balances:
        for w in wallets:
            addr = w.get("address")
            if not addr:
                continue
            per_chain: Dict[str, Any] = {}
            for ch in ("base", "arbitrum", "ethereum", "optimism", "polygon", "bsc"):
                try:
                    val = await asyncio.to_thread(chains.native_balance, ch, addr)
                    per_chain[ch] = f"{val:.6f}"
                except Exception:
                    per_chain[ch] = "н/д"
            balances[w["name"]] = per_chain

    return templates.TemplateResponse(
        request,
        "wallets.html",
        {
            "page": "wallets",
            "msg": msg,
            "err": err,
            "wallets": wallets,
            "reveal": reveal.strip(),
            "balances": balances,
            "chains": list(CHAIN_IDS.keys()),
            "passphrase": ensure_panel_passphrase(),
        },
    )


@app.get("/forecast", response_class=HTMLResponse)
async def forecast_page(
    request: Request,
    hours: float = Query(default=1.0),
    budget: float = Query(default=0.0),
    services_income: int = Query(default=1),
) -> HTMLResponse:
    data = await forecast.build_project_forecast(
        hours_per_day=hours,
        budget_usd=budget,
        include_services=bool(services_income),
    )
    checklist = await forecast.get_checklist_state()
    return templates.TemplateResponse(
        request,
        "forecast.html",
        {
            "page": "forecast",
            "forecast": data,
            "streams": forecast.EARNING_STREAMS,
            "checklist": checklist,
        },
    )


@app.get("/drops", response_class=HTMLResponse)
async def drops_page(
    request: Request,
    tab: str = Query(default="all"),
    msg: str = Query(default=""),
    err: str = Query(default=""),
) -> HTMLResponse:
    if tab == "watching":
        rows = await db.list_projects(status="watching", limit=80)
    elif tab == "new":
        rows = await db.list_projects(status="new", limit=80)
    else:
        rows = await db.list_projects(limit=80)
    if tab == "quick":
        rows = [r for r in rows if r.get("category") in {"quick-earn", "airdrop-listing"} or r.get("source") == "starter"]

    elig_rows = await db.list_eligibility(limit=40)
    return templates.TemplateResponse(
        request,
        "drops.html",
        {
            "page": "drops",
            "tab": tab,
            "rows": rows,
            "elig_rows": elig_rows,
            "msg": msg,
            "err": err,
        },
    )


@app.get("/guide", response_class=HTMLResponse)
async def guide_page(request: Request) -> HTMLResponse:
    checklist = await forecast.get_checklist_state()
    wallets = _enrich_wallets()
    return templates.TemplateResponse(
        request,
        "guide.html",
        {
            "page": "guide",
            "checklist": checklist,
            "wallets": wallets,
        },
    )


# --------------------------------------------------------------------- #
# Actions (POST forms)
# --------------------------------------------------------------------- #
@app.post("/wallets/create")
async def create_wallet_post(name: str = Form(...)):
    ensure_panel_passphrase()
    clean = (name or "").strip()
    try:
        info = wallet.create_wallet(clean)
        await forecast.toggle_checklist_item("step_wallet", True)
        return RedirectResponse(
            f"/wallets?reveal={info['name']}&msg=Кошелёк+{info['name']}+создан!+Ниже+открыты+12+слов+для+подключения+в+Exodus.",
            status_code=303,
        )
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(f"/wallets?err={str(exc)}", status_code=303)


@app.post("/wallets/import")
async def import_wallet_post(
    name: str = Form(...),
    secret_value: str = Form(...),
):
    ensure_panel_passphrase()
    clean_name = (name or "").strip()
    raw = (secret_value or "").strip()
    try:
        if " " in raw:
            info = wallet.import_mnemonic(clean_name, raw)
        else:
            info = wallet.import_key(clean_name, raw)
        await forecast.toggle_checklist_item("step_wallet", True)
        return RedirectResponse(
            f"/wallets?msg=Кошелёк+{info['name']}+({info['address'][:10]}…)+успешно+импортирован",
            status_code=303,
        )
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(f"/wallets?err={str(exc)}", status_code=303)


@app.post("/services/action")
async def service_action_post(
    service: str = Form(...),
    action: str = Form(...),
):
    if action == "start":
        res = supervisor.start_service(service)
    elif action == "stop":
        res = supervisor.stop_service(service)
    elif action == "pause_toggle":
        cur = await db.get_kv("paused", "0") == "1"
        await db.set_kv("paused", "0" if cur else "1")
        res = {"ok": True, "message": "Агент снят с паузы" if cur else "Агент поставлен на паузу"}
    else:
        res = {"ok": False, "message": "Неизвестное действие"}
    key = "msg" if res.get("ok") else "err"
    return RedirectResponse(f"/?{key}={res.get('message', '')}", status_code=303)


@app.post("/bot/configure")
async def configure_bot_post(
    bot_token: str = Form(default=""),
    admin_ids: str = Form(default=""),
):
    updates: Dict[str, str] = {}
    if bot_token.strip():
        updates["AIRDROP_BOT_TOKEN"] = bot_token.strip()
        settings.bot_token = bot_token.strip()
    if admin_ids.strip():
        updates["AIRDROP_ADMIN_IDS"] = admin_ids.strip()
        settings.admin_ids = [int(x.strip()) for x in admin_ids.split(",") if x.strip().isdigit()]
    if not updates:
        return RedirectResponse("/?err=Укажите+токен+бота+и+ваш+Telegram+ID", status_code=303)
    _update_env_vars(updates)
    supervisor.stop_service("airdrop-bot")
    res = supervisor.start_service("airdrop-bot")
    await forecast.toggle_checklist_item("step_bot", True)
    key = "msg" if res.get("ok") else "err"
    return RedirectResponse(f"/?{key}={res.get('message', 'Настройки сохранены')}", status_code=303)


@app.post("/scan/run")
async def run_scan_post(
    mode: str = Form(default="online"),
    return_to: str = Form(default="/"),
):
    offline = mode == "offline"
    try:
        res = await discovery_pass(offline=offline)
        await forecast.seed_starter_opportunities()
        msg = f"Поиск завершён: обработано {res.get('total', 0)}, новых {res.get('new', 0)}"
        return RedirectResponse(f"{return_to}?msg={msg}", status_code=303)
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(f"{return_to}?err={str(exc)[:200]}", status_code=303)


@app.post("/eligibility/run")
async def run_eligibility_post(return_to: str = Form(default="/drops")):
    ensure_panel_passphrase()
    try:
        res = await eligibility_pass()
        msg = f"Проверено кошельков/проектов: {res.get('checked', 0)}, найдено eligible: {res.get('eligible', 0)}"
        return RedirectResponse(f"{return_to}?msg={msg}", status_code=303)
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(f"{return_to}?err={str(exc)[:200]}", status_code=303)


@app.post("/drops/status")
async def drop_status_post(
    slug: str = Form(...),
    status: str = Form(...),
    return_to: str = Form(default="/drops"),
):
    await db.set_project_status(slug, status)
    if status == "watching":
        await forecast.toggle_checklist_item("step_scan_watch", True)
    return RedirectResponse(f"{return_to}?msg=Статус+обновлён+({status})", status_code=303)


@app.post("/drops/add")
async def drop_add_post(
    name: str = Form(...),
    url: str = Form(default=""),
    chain: str = Form(default="base"),
    category: str = Form(default="quick-earn"),
    summary: str = Form(default=""),
    check_url: str = Form(default=""),
):
    clean = (name or "").strip()
    if not clean:
        return RedirectResponse("/drops?err=Укажите+название+проекта", status_code=303)
    item = Airdrop(
        name=clean,
        url=url.strip(),
        source="manual",
        chain=chain.strip().lower(),
        category=category.strip(),
        score=9.0,
        summary=summary.strip() or "Добавлено вручную оператором.",
        extra={"check_url": check_url.strip()} if check_url.strip() else {},
    )
    await db.upsert_project(item, score=9.0)
    await db. set_project_status(item.slug, "watching")
    return RedirectResponse(f"/drops?tab=watching&msg=Проект+{clean}+добавлен+в+работу", status_code=303)


@app.post("/checklist/toggle")
async def checklist_toggle_post(
    item_id: str = Form(...),
    return_to: str = Form(default="/"),
):
    await forecast.toggle_checklist_item(item_id)
    return RedirectResponse(return_to, status_code=303)


# --------------------------------------------------------------------- #
# JSON API for monitoring / healthchecks
# --------------------------------------------------------------------- #
@app.get("/api/health", response_class=JSONResponse)
async def api_health() -> JSONResponse:
    services = await supervisor.services_status()
    stats = await db.stats()
    wallets = wallet.list_wallets()
    return JSONResponse(
        {
            "ok": True,
            "services": services,
            "stats": stats,
            "wallets_count": len(wallets),
            "wallets": [{"name": w["name"], "address": w.get("address", "")} for w in wallets],
        }
    )
