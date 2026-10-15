from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

from . import db


_CONTROL_ROOT = db.DATA_DIR / "farm_control"
_COMMAND_ID_RE = re.compile(r"^[a-f0-9]{32}$")


def _process_dir(pid: int) -> Path:
    process_id = int(pid)
    if process_id <= 0:
        raise ValueError("PID должен быть положительным числом")
    return _CONTROL_ROOT / str(process_id)


def inbox_dir(pid: int) -> Path:
    return _process_dir(pid) / "inbox"


def outbox_dir(pid: int) -> Path:
    return _process_dir(pid) / "outbox"


def status_path(pid: int) -> Path:
    return _process_dir(pid) / "status.json"


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        temporary.unlink(missing_ok=True)


def write_command(pid: int, action: str, payload: dict[str, Any] | None = None) -> str:
    command_id = uuid.uuid4().hex
    _atomic_json(inbox_dir(pid) / f"{command_id}.json", {
        "id": command_id,
        "action": str(action),
        "payload": payload if isinstance(payload, dict) else {},
    })
    return command_id


def read_commands(pid: int) -> list[dict[str, Any]]:
    directory = inbox_dir(pid)
    if not directory.is_dir():
        return []
    commands: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json"), key=lambda item: item.name):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if (
                isinstance(data, dict)
                and _COMMAND_ID_RE.fullmatch(str(data.get("id") or ""))
                and path.stem == data.get("id")
                and isinstance(data.get("payload"), dict)
            ):
                commands.append(data)
        except (OSError, json.JSONDecodeError):
            continue
        finally:
            path.unlink(missing_ok=True)
    return commands


def write_result(pid: int, command_id: str, result: dict[str, Any]) -> None:
    if not _COMMAND_ID_RE.fullmatch(str(command_id)):
        raise ValueError("Некорректный ID команды")
    _atomic_json(outbox_dir(pid) / f"{command_id}.json", result)


def read_result(pid: int, command_id: str) -> dict[str, Any] | None:
    if not _COMMAND_ID_RE.fullmatch(str(command_id)):
        return None
    path = outbox_dir(pid) / f"{command_id}.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        path.unlink(missing_ok=True)
        return None
    path.unlink(missing_ok=True)
    return value if isinstance(value, dict) else None


def cancel_command(pid: int, command_id: str) -> None:
    if not _COMMAND_ID_RE.fullmatch(str(command_id)):
        return
    (inbox_dir(pid) / f"{command_id}.json").unlink(missing_ok=True)


def publish_status(pid: int, status: dict[str, Any]) -> None:
    value = dict(status)
    value["pid"] = int(pid)
    _atomic_json(status_path(pid), value)


def read_status(pid: int) -> dict[str, Any] | None:
    try:
        value = json.loads(status_path(pid).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) and int(value.get("pid") or 0) == int(pid) else None


def clear_process_files(pid: int) -> None:
    directory = _process_dir(pid)
    if not directory.exists():
        return
    for path in directory.rglob("*"):
        if path.is_file():
            path.unlink(missing_ok=True)
    for path in sorted(directory.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_dir():
            path.rmdir()
    directory.rmdir()
