"""Load and merge config.json with sane defaults. Values may be overridden by
environment variables so a token never has to live in the file."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "token": "",
    "db_path": "data/modbot.sqlite3",
    "log_channel_id": 0,
    "dm_target": True,
    "default_reason": "Причина не указана",
    "feedback_hint": "Не согласны с наказанием — обсудите его в канале обратной связи или заведите тикет.",
    "mute_role_id": 0,
    "dry_run": False,

    # ---- устойчивость соединения (бесплатный хостинг, обрывы, DNS)
    "keepalive": "auto",  # true | false | auto: поднимать /healthz на $PORT
    "keepalive_port": 8080,

    # id вашего сервера: команды грузятся только в него — видны сразу, без
    # глобального лимита и часа на распространение. Пусто = глобально.
    # Можно и переменной окружения DISCORD_SYNC_GUILD.
    "sync_guild_id": os.getenv("DISCORD_SYNC_GUILD", ""),
    "connect_backoff": [1, 5, 20, 60],  # попытки самого шлюза Discord
    "max_connect_retries": 20,
    "reconnect_delays": [5, 15, 30, 60, 120, 300],  # паузы супервизора, сек
    "reconnect_attempts": 0,  # 0 = перезапускать вечно
    "reconnect_reset_after": 300,  # столько секунд аптайма обнуляет счётчик

    "drivers": {"active": "discord_voice", "rcon": {}},
    "warn_thresholds": [],

    # ---- события наказаний, выданных В ИГРЕ (cogs/gamefeed.py)
    "gamefeed": {
        "enabled": False,             # включить может только админ, знающий пути к логам
        "source": "log",              # "log" — хвост файла; "rcon" — опрос banlist
        "log_file": "",               # /var/minecraft/logs/latest.log
        "poll_cmd": "banlist",
        "poll_action": "ban",
        "interval_s": 5,              # не чаще этого (пол)
        "patterns": None,             # None = дефолты core/gamefeed.py
    },
}


class Config(dict):
    __getattr__ = dict.get

    def __init__(self, data: dict):
        merged = json.loads(json.dumps(DEFAULTS))
        deep_update(merged, data)
        merged["token"] = os.getenv("DISCORD_TOKEN") or merged.get("token") or ""
        super().__init__(merged)


def deep_update(dst: dict, src: dict) -> None:
    for key, value in src.items():
        if isinstance(dst.get(key), dict) and isinstance(value, dict):
            deep_update(dst[key], value)
        else:
            dst[key] = value


#: значения, которые никто не должен считать настоящим токеном
PLACEHOLDERS = {"", "PASTE_BOT_TOKEN_HERE", "YOUR_BOT_TOKEN", "TOKEN", "changeme"}


def load(config_path: str | Path = "config.json", *, require_token: bool = True) -> Config:
    path: Path | None = Path(config_path)
    if not path.exists():
        example = path.with_name("config.json.example")
        path = example if example.exists() else None
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path else {}
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        # типичный первый запуск: конфиг правят руками и ломают запятую
        raise SystemExit(
            f"Не смог прочитать {path}: {exc}"
            "\nПроверьте JSON (запятые/кавычки) или удалите файл — тогда"
            " возьмём config.json.example и переменные окружения."
        ) from None
    cfg = Config(data)
    if require_token and str(cfg.get("token", "")).strip() in PLACEHOLDERS:
        raise SystemExit(
            "Токен не задан. Вариант 1 (быстрее для хостинга): задайте переменную"
            " окружения DISCORD_TOKEN — файл не нужен вовсе.\n"
            "Вариант 2: скопируйте config.json.example в config.json и впишите token"
            " из Discord Developer Portal (Bot → Reset Token)."
        )
    return cfg
