"""Monitor and control all 4 project services from the :1111 web panel.

Services tracked:
1. ``airdrop-panel`` — Web control center on port 1111 (`airdrop_web.py`)
2. ``0x4-web``       — Telegram accounts & chat-farm panel on port 8080 (`web_run.py`)
3. ``airdrop-agent`` — 24/7 discovery & eligibility daemon (`python -m airdrop.agent --loop`)
4. ``airdrop-bot``   — Telegram bot (`bot_airdrop.py` / `bot.py`)
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import db
from .settings import ROOT, settings

_MANAGED: Dict[str, subprocess.Popen] = {}


def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _find_pids(pattern: str) -> List[int]:
    """Scan /proc (or ps) for processes whose command line contains `pattern`."""
    pids: List[int] = []
    my_pid = os.getpid()
    proc = Path("/proc")
    if proc.exists():
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            if pid == my_pid:
                continue
            try:
                cmdline = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "ignore")
                stat_text = (entry / "stat").read_text(encoding="utf-8", errors="ignore")
                # ignore zombies
                if " Z " in stat_text:
                    continue
            except OSError:
                continue
            if pattern in cmdline and "python" in cmdline:
                pids.append(pid)
    return pids


def _systemd_active(unit: str) -> Optional[bool]:
    if not Path("/run/systemd/system").exists():
        return None
    try:
        res = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
        return res.returncode == 0 and res.stdout.strip() == "active"
    except Exception:
        return None


def _python_bin() -> str:
    venv_py = ROOT / ".venv" / "bin" / "python"
    if venv_py.exists():
        return str(venv_py)
    return sys.executable


def _env_map() -> Dict[str, str]:
    env = dict(os.environ)
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def start_service(name: str) -> Dict[str, Any]:
    """Start one of the sibling services if not already running."""
    settings.ensure_dirs()
    env = _env_map()
    py = _python_bin()

    # Prefer systemd if unit is installed and we are root
    unit_map = {
        "airdrop-agent": "airdrop-agent.service",
        "airdrop-bot": "airdrop-bot.service",
        "0x4-web": "0x4-web.service",
    }
    unit = unit_map.get(name)
    if unit and os.geteuid() == 0 and Path(f"/etc/systemd/system/{unit}").exists():
        res = subprocess.run(["systemctl", "restart", unit], capture_output=True, text=True, timeout=10)
        if res.returncode == 0:
            return {"ok": True, "service": name, "mode": "systemd", "message": f"{unit} перезапущен через systemd"}

    log_dir = settings.data_dir
    log_dir.mkdir(parents=True, exist_ok=True)

    if name == "airdrop-agent":
        pids = _find_pids("airdrop.agent")
        if pids:
            return {"ok": True, "service": name, "pid": pids[0], "message": f"Агент уже работает (PID {pids[0]})"}
        log_f = open(log_dir / "agent.log", "a", encoding="utf-8")
        proc = subprocess.Popen(
            [py, "-m", "airdrop.agent", "--loop"],
            cwd=str(ROOT),
            env=env,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        _MANAGED[name] = proc
        return {"ok": True, "service": name, "pid": proc.pid, "message": f"Агент запущен (PID {proc.pid})"}

    if name == "0x4-web":
        web_port = int(env.get("WEB_PORT", "8080") or "8080")
        if _port_open(web_port):
            return {"ok": True, "service": name, "message": f"Панель 0x4 уже слушает порт {web_port}"}
        log_f = open(log_dir / "web8080.log", "a", encoding="utf-8")
        proc = subprocess.Popen(
            [py, "web_run.py"],
            cwd=str(ROOT),
            env=env,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        _MANAGED[name] = proc
        return {"ok": True, "service": name, "pid": proc.pid, "message": f"Панель 0x4 (:8080) запущена (PID {proc.pid})"}

    if name == "airdrop-bot":
        token = env.get("AIRDROP_BOT_TOKEN", "").strip()
        admins = env.get("AIRDROP_ADMIN_IDS", "").strip() or env.get("ADMIN_IDS", "").strip()
        if not token or not admins:
            return {
                "ok": False,
                "service": name,
                "message": "Сначала укажите AIRDROP_BOT_TOKEN и ваш Telegram ID (AIRDROP_ADMIN_IDS) в форме ниже.",
            }
        pids = _find_pids("bot_airdrop.py")
        if pids:
            return {"ok": True, "service": name, "pid": pids[0], "message": f"Telegram-бот уже работает (PID {pids[0]})"}
        log_f = open(log_dir / "bot_airdrop.log", "a", encoding="utf-8")
        proc = subprocess.Popen(
            [py, "bot_airdrop.py"],
            cwd=str(ROOT),
            env=env,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        _MANAGED[name] = proc
        return {"ok": True, "service": name, "pid": proc.pid, "message": f"Telegram-бот запущен (PID {proc.pid})"}

    return {"ok": False, "service": name, "message": f"Неизвестный сервис: {name}"}


def stop_service(name: str) -> Dict[str, Any]:
    unit_map = {
        "airdrop-agent": ("airdrop-agent.service", "airdrop.agent"),
        "airdrop-bot": ("airdrop-bot.service", "bot_airdrop.py"),
        "0x4-web": ("0x4-web.service", "web_run.py"),
    }
    if name not in unit_map:
        return {"ok": False, "service": name, "message": f"Нельзя остановить {name}"}
    unit, pattern = unit_map[name]
    if os.geteuid() == 0 and Path(f"/etc/systemd/system/{unit}").exists():
        subprocess.run(["systemctl", "stop", unit], capture_output=True, text=True, timeout=10)
    pids = _find_pids(pattern)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    _MANAGED.pop(name, None)
    return {"ok": True, "service": name, "message": f"Сервис {name} остановлен"}


def ensure_sibling_services() -> List[Dict[str, Any]]:
    """Auto-start sibling services when requested (default ON)."""
    out: List[Dict[str, Any]] = []
    if os.getenv("AIRDROP_AUTOSTART_SIBLINGS", "1").lower() in {"0", "false", "no"}:
        return out
    for svc in ("0x4-web", "airdrop-agent", "airdrop-bot"):
        try:
            out.append(start_service(svc))
        except Exception as exc:  # noqa: BLE001
            out.append({"ok": False, "service": svc, "message": str(exc)})
    return out


async def services_status() -> List[Dict[str, Any]]:
    """Return status cards for all 4 services."""
    env = _env_map()
    panel_port = int(env.get("AIRDROP_PANEL_PORT", "1111") or "1111")
    web_port = int(env.get("WEB_PORT", "8080") or "8080")
    paused = await db.get_kv("paused", "0") == "1"
    last_disc = await db.last_run("discovery")
    last_elig = await db.last_run("eligibility")

    agent_pids = _find_pids("airdrop.agent")
    bot_pids = _find_pids("bot_airdrop.py")
    main_bot_pids = _find_pids("bot.py")
    web_pids = _find_pids("web_run.py")
    farm_pids = _find_pids("farm.py")
    web8080_up = _port_open(web_port) or bool(web_pids)
    bot_configured = bool(env.get("AIRDROP_BOT_TOKEN", "").strip() and (env.get("AIRDROP_ADMIN_IDS", "").strip() or env.get("ADMIN_IDS", "").strip()))

    return [
        {
            "id": "airdrop-panel",
            "number": 1,
            "title": f"Панель заработка и кошельков (:{panel_port})",
            "port": panel_port,
            "running": True,
            "status_label": "РАБОТАЕТ (ONLINE)",
            "status_color": "success",
            "pids": [os.getpid()],
            "detail": f"Порт {panel_port} открыт · Кошельки, Exodus-экспорт, Прогноз и Мониторинг",
            "controllable": False,
        },
        {
            "id": "0x4-web",
            "number": 2,
            "title": f"Панель Telegram-фермы 0x4 (:{web_port})",
            "port": web_port,
            "running": web8080_up,
            "status_label": "РАБОТАЕТ (ONLINE)" if web8080_up else "ОСТАНОВЛЕНА",
            "status_color": "success" if web8080_up else "warning",
            "pids": web_pids,
            "detail": (
                f"Порт {web_port} · Аккаунты Pyrogram и чат-ферма"
                + (f" · farm.py активна (PID {farm_pids[0]})" if farm_pids else " · farm.py в ожидании")
            ),
            "controllable": True,
        },
        {
            "id": "airdrop-agent",
            "number": 3,
            "title": "Агент поиска аирдропов и проверок (24/7)",
            "port": None,
            "running": bool(agent_pids) and not paused,
            "status_label": (
                "НА ПАУЗЕ" if paused else ("РАБОТАЕТ (24/7)" if agent_pids else "ОСТАНОВЛЕН")
            ),
            "status_color": "warning" if paused else ("success" if agent_pids else "danger"),
            "pids": agent_pids,
            "detail": (
                f"Последний поиск: {(last_disc or {}).get('ts', 'ещё не запускался')} · "
                f"Проверка кошельков: {(last_elig or {}).get('ts', 'ещё не запускалась')}"
            ),
            "controllable": True,
        },
        {
            "id": "airdrop-bot",
            "number": 4,
            "title": "Telegram-бот управления (bot_airdrop.py)",
            "port": None,
            "running": bool(bot_pids or main_bot_pids),
            "status_label": (
                "РАБОТАЕТ (ONLINE)"
                if (bot_pids or main_bot_pids)
                else ("ЖДЁТ ТОКЕН ОТ @BotFather" if not bot_configured else "ОСТАНОВЛЕН")
            ),
            "status_color": "success" if (bot_pids or main_bot_pids) else "warning",
            "pids": bot_pids + main_bot_pids,
            "detail": (
                "Токен настроен, бот принимает команды в Telegram"
                if (bot_pids or main_bot_pids)
                else "Вставьте AIRDROP_BOT_TOKEN и ваш Telegram ID ниже — бот запустится в 1 клик"
            ),
            "controllable": True,
        },
    ]
