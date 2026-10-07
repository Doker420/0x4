from __future__ import annotations

import json
import math
import re
from typing import Any, Mapping

MEDIA_KINDS = ("text", "gif", "sticker", "photo", "voice")
DEFAULT_MEDIA_BIAS = {
    "text": 0.60,
    "gif": 0.12,
    "sticker": 0.12,
    "photo": 0.08,
    "voice": 0.08,
}

DEFAULT_FARM_SETTINGS: dict[str, Any] = {
    "agent_prompt": "Отвечай естественно, по теме сообщения и коротко. Не выдавай себя за другого человека.",
    "min_delay_sec": 2.0,
    "max_delay_sec": 8.0,
    "default_reply_probability": 0.85,
    "reaction_probability": 0.35,
    "qa_probability": 0.25,
    "clone_probability": 0.25,
    "proactive_enabled": False,
    "followups_enabled": True,
    "followups_max": 2,
    "typing_simulation": True,
    "deepseek_model": "default",
    "deepseek_thinking": False,
    "deepseek_search": False,
    "default_media_bias": DEFAULT_MEDIA_BIAS.copy(),
    "scenario_mode": "reactive",
    "scenario_topic": "",
    "scenario_turns": 20,
    "joke_every": 5,
    "rest_every": 6,
    "rest_min_sec": 60,
    "rest_max_sec": 120,
    "roulette_numbers": "0-36",
    "post_opening": True,
    "night_mode_enabled": False,
    "night_mode_start": "23:00",
    "night_mode_end": "07:00",
    "idle_enabled": False,
    "idle_after_sec": 900,
    "idle_cooldown_sec": 600,
    "idle_gif_percent": 70,
    "gif_share_percent": 25,
    "music_enabled": False,
    "music_source": "@sad_tracky",
    "music_share_percent": 10,
}


def normalize_media_bias(value: Any, fallback: Mapping[str, Any] | None = None) -> dict[str, float]:
    """Parse and normalize media weights to a probability distribution."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            value = {}
    if not isinstance(value, Mapping):
        value = {}

    fallback_values = dict(fallback or DEFAULT_MEDIA_BIAS)
    weights: dict[str, float] = {}
    for kind in MEDIA_KINDS:
        raw = value.get(kind, fallback_values.get(kind, 0.0))
        try:
            number = float(raw)
        except (TypeError, ValueError):
            number = 0.0
        weights[kind] = number if math.isfinite(number) and number >= 0 else 0.0

    total = sum(weights.values())
    if total <= 0:
        weights = {kind: float(fallback_values.get(kind, 0.0)) for kind in MEDIA_KINDS}
        total = sum(max(0.0, item) for item in weights.values())
    if total <= 0:
        weights = {kind: float(kind == "text") for kind in MEDIA_KINDS}
        total = 1.0
    return {kind: max(0.0, weights[kind]) / total for kind in MEDIA_KINDS}


def parse_roulette_numbers(value: Any) -> list[int]:
    """Parse a bounded roulette pool such as ``0-36`` or ``2, 7, 18``."""
    text = str(value or "").strip()
    if not text or len(text) > 256:
        raise ValueError("Укажите диапазон чисел (например, 0-36) или список через запятую")

    match = re.fullmatch(r"\s*(\d+)\s*[-–]\s*(\d+)\s*", text)
    if match:
        start, end = map(int, match.groups())
        if end < start:
            raise ValueError("В диапазоне чисел конец должен быть не меньше начала")
        if end > 10000:
            raise ValueError("Числа рулетки должны быть в диапазоне 0–10000")
        if end - start > 999:
            raise ValueError("Диапазон рулетки не может содержать больше 1000 чисел")
        return list(range(start, end + 1))

    parts = [part for part in re.split(r"[,;\s]+", text) if part]
    if not parts or len(parts) > 100:
        raise ValueError("Список рулетки должен содержать от 1 до 100 чисел")
    if any(not re.fullmatch(r"\d+", part) for part in parts):
        raise ValueError("Введите диапазон чисел или список целых чисел через запятую")
    numbers = list(dict.fromkeys(int(part) for part in parts))
    if any(number > 10000 for number in numbers):
        raise ValueError("Числа рулетки должны быть в диапазоне 0–10000")
    return numbers


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def load_farm_settings(raw: str | Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return validated settings, using defaults for missing/invalid values."""
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {}
    else:
        parsed = raw
    if not isinstance(parsed, Mapping):
        parsed = {}

    settings = DEFAULT_FARM_SETTINGS.copy()
    settings.update({key: value for key, value in parsed.items() if key in settings})

    for key in ("min_delay_sec", "max_delay_sec"):
        try:
            value = float(settings[key])
        except (TypeError, ValueError):
            value = float(DEFAULT_FARM_SETTINGS[key])
        settings[key] = value if math.isfinite(value) else float(DEFAULT_FARM_SETTINGS[key])
    settings["min_delay_sec"] = max(0.0, min(settings["min_delay_sec"], 86400.0))
    settings["max_delay_sec"] = max(settings["min_delay_sec"], min(settings["max_delay_sec"], 86400.0))

    for key in (
        "default_reply_probability",
        "reaction_probability",
        "qa_probability",
        "clone_probability",
    ):
        try:
            value = float(settings[key])
        except (TypeError, ValueError):
            value = float(DEFAULT_FARM_SETTINGS[key])
        settings[key] = max(0.0, min(1.0, value)) if math.isfinite(value) else float(DEFAULT_FARM_SETTINGS[key])

    settings["agent_prompt"] = str(settings.get("agent_prompt") or DEFAULT_FARM_SETTINGS["agent_prompt"]).strip()[:5000]
    settings["proactive_enabled"] = _as_bool(settings.get("proactive_enabled"))
    settings["followups_enabled"] = _as_bool(settings.get("followups_enabled"))
    settings["followups_max"] = _bounded_int(settings.get("followups_max"), 2, 0, 3)
    settings["typing_simulation"] = _as_bool(settings.get("typing_simulation"))
    settings["deepseek_thinking"] = _as_bool(settings.get("deepseek_thinking"))
    settings["deepseek_search"] = _as_bool(settings.get("deepseek_search"))
    settings["deepseek_model"] = str(settings.get("deepseek_model") or "default")[:32]
    settings["default_media_bias"] = normalize_media_bias(
        settings.get("default_media_bias"), DEFAULT_MEDIA_BIAS
    )

    mode = str(settings.get("scenario_mode") or "reactive").strip().lower()
    settings["scenario_mode"] = mode if mode in {"reactive", "discussion", "roulette", "combined", "history_dialogue"} else "reactive"
    settings["scenario_topic"] = str(settings.get("scenario_topic") or "").strip()[:2000]
    settings["scenario_turns"] = _bounded_int(settings.get("scenario_turns"), 20, 0, 500)
    settings["joke_every"] = _bounded_int(settings.get("joke_every"), 5, 0, 1000)
    settings["rest_every"] = _bounded_int(settings.get("rest_every"), 6, 0, 1000)
    settings["rest_min_sec"] = _bounded_int(settings.get("rest_min_sec"), 60, 0, 86400)
    settings["rest_max_sec"] = _bounded_int(settings.get("rest_max_sec"), 120, 0, 86400)
    settings["rest_max_sec"] = max(settings["rest_min_sec"], settings["rest_max_sec"])
    settings["roulette_numbers"] = str(settings.get("roulette_numbers") or "0-36").strip()[:256]
    settings["post_opening"] = _as_bool(settings.get("post_opening"))
    settings["night_mode_enabled"] = _as_bool(settings.get("night_mode_enabled"))
    settings["night_mode_start"] = parse_clock(
        settings.get("night_mode_start"), DEFAULT_FARM_SETTINGS["night_mode_start"]
    )
    settings["night_mode_end"] = parse_clock(
        settings.get("night_mode_end"), DEFAULT_FARM_SETTINGS["night_mode_end"]
    )

    settings["idle_enabled"] = _as_bool(settings.get("idle_enabled"))
    settings["idle_after_sec"] = _bounded_int(settings.get("idle_after_sec"), 900, 60, 86400)
    settings["idle_cooldown_sec"] = _bounded_int(settings.get("idle_cooldown_sec"), 600, 60, 86400)
    settings["idle_gif_percent"] = _percent(settings.get("idle_gif_percent"), 70)
    settings["gif_share_percent"] = _percent(settings.get("gif_share_percent"), 25)
    settings["music_enabled"] = _as_bool(settings.get("music_enabled"))
    settings["music_source"] = chat_reference(
        settings.get("music_source"), DEFAULT_FARM_SETTINGS["music_source"]
    )
    settings["music_share_percent"] = _percent(settings.get("music_share_percent"), 10)
    return settings


def parse_clock(value: Any, fallback: str) -> str:
    """Validate a UTC wall-clock time given as ``HH:MM``."""
    minutes = clock_to_minutes(value)
    if minutes is None:
        return fallback
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def clock_to_minutes(value: Any) -> int | None:
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", str(value or "").strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None
    return hour * 60 + minute


def _percent(value: Any, default: int) -> int:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float(default)
    if not math.isfinite(number):
        number = float(default)
    return int(max(0, min(100, round(number))))


def chat_reference(value: Any, fallback: str = "") -> str:
    """Keep a chat reference as @username or a numeric id, without URLs."""
    text = str(value or "").strip()
    if not text:
        return fallback
    if text.startswith("http") or text.startswith("t.me"):
        # Accept a pasted link by keeping only the chat name, never the URL itself.
        segments = [part for part in re.split(r"[/#?]", text) if part]
        candidate = ""
        for segment in segments:
            if segment.casefold() in {"http:", "https:", "t.me", "www.t.me", "c", "joinchat", "s"}:
                continue
            candidate = segment
            break
        text = candidate
        if text and not text.startswith("@") and not text.lstrip("-").isdigit():
            text = f"@{text}"
    text = text.strip()
    if re.fullmatch(r"@[A-Za-z0-9_]{3,64}", text):
        return text
    if re.fullmatch(r"-?\d{5,20}", text):
        return text
    return fallback


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}
