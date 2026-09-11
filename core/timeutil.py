"""Parsing and formatting of human friendly durations: 10m, 1h30m, 7d, 90."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

UNIT_SECONDS = {
    "s": 1,
    "sec": 1,
    "сек": 1,
    "m": 60,
    "min": 60,
    "мин": 60,
    "h": 3600,
    "hr": 3600,
    "hour": 3600,
    "ч": 3600,
    "час": 3600,
    "d": 86400,
    "day": 86400,
    "д": 86400,
    "день": 86400,
    "w": 604800,
    "week": 604800,
    "нед": 604800,
}

_TOKEN = re.compile(r"(\d+(?:[.,]\d+)?)\s*([a-zа-я]{1,5})", re.IGNORECASE)
_MAX = 356 * 86400


class DurationError(ValueError):
    """Raised when a duration string cannot be parsed."""


def parse_duration(text: str | None) -> timedelta:
    """Turn "1h30m" / "3d" / "1.5h" / "90" / "perm" into a timedelta.

    Empty, "0", "perm", "infinite", "forever" return timedelta.max (permanent).
    """
    if text is None:
        raise DurationError("укажите длительность, например 10m, 1h, 7d или perm")

    raw = text.strip().lower().replace("_", "")
    if not raw:
        raise DurationError("укажите длительность, например 10m, 1h, 7d или perm")
    if raw in {"perm", "permanent", "infinite", "forever", "0", "навсегда", "∞"}:
        return timedelta.max

    if re.fullmatch(r"\d+[.,]?\d*", raw):
        # bare number: decimal comma is a European habit, not a thousands sep
        return timedelta(seconds=float(raw.replace(",", ".")))

    seconds = 0.0
    matched = False
    pos = 0
    for m in _TOKEN.finditer(raw):
        matched = True
        if m.start() != pos:  # garbage between tokens ("1h*30m") must not pass
            raise DurationError(f"не понял длительность «{text}». Формат: 30m, 2h, 7d, 1h30m, perm")
        pos = m.end()
        number, unit = m.group(1), m.group(2)
        mult = next((v for k, v in UNIT_SECONDS.items() if unit.startswith(k)), None)
        if mult is None:
            raise DurationError(f"неизвестная единица «{unit}». Доступны: s, m, h, d, w")
        seconds += float(number.replace(",", ".")) * mult
    if not matched or pos != len(raw):
        raise DurationError(f"не понял длительность «{text}». Формат: 30m, 2h, 7d, 1h30m, perm")

    if seconds <= 0:
        return timedelta.max
    if seconds > _MAX:
        raise DurationError("максимальный срок — 356 дней (ограничение Discord)")
    return timedelta(seconds=seconds)



def is_permanent(delta: timedelta) -> bool:
    """Only parse_duration's "perm" sentinel means forever.

    The cutoff used to be ">= 350 days", which silently turned a 400-day warn
    into a permanent one — Discord's 356-day limit applies to timeouts, not to
    our own bookkeeping.
    """
    return delta >= timedelta.max


def expires_at(delta: timedelta) -> datetime | None:
    """Absolute UTC expiry, or None for punishments that never expire."""
    if is_permanent(delta):
        return None
    return datetime.now(timezone.utc) + delta


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def discord_relative(dt: datetime | None) -> str:
    """"<t:...:R>" mention usable in chat, or «навсегда»."""
    if dt is None:
        return "навсегда"
    stamp = int(dt.timestamp())
    return f"<t:{stamp}:R> (<t:{stamp}:f>)"


UNITS = (
    ("день", "дня", "дней", 86400),
    ("час", "часа", "часов", 3600),
    ("минута", "минуты", "минут", 60),
    ("секунда", "секунды", "секунд", 1),
)


def _plural(value: int, forms: tuple[str, str, str]) -> str:
    one, few, many = forms
    mod10, mod100 = value % 10, value % 100
    if mod100 in (11, 12, 13, 14):
        return many
    if mod10 == 1:
        return one
    if 2 <= mod10 <= 4:
        return few
    return many


def humanize_long(delta: timedelta) -> str:
    """Full spoken duration for the ВРЕМЯ column: 3600s -> "1 час".

    Discord.py timeouts and game mutes are reported to players in words, not in
    "1h", so this spells the two largest non-zero units out.
    """
    if is_permanent(delta):
        return "навсегда"
    total = int(round(delta.total_seconds()))
    if total <= 0:
        return "0 секунд"
    parts: list[str] = []
    for one, few, many, secs in UNITS:
        count, total = divmod(total, secs)
        if count:
            parts.append(f"{count} {_plural(count, (one, few, many))}")
        if len(parts) == 2:
            break
    return " ".join(parts)


def compact_english(delta: timedelta) -> str:
    """`30m`, `1h30m`, `7d` — формат, который понимают игровые плагины сроков.

    Нужен именно ASCII: EssentialsX/LuckPerms не распознают «1 час 30 минут».
    """
    if is_permanent(delta):
        return ""
    total = int(round(delta.total_seconds()))
    if total <= 0:
        return "1s"
    parts = []
    for unit, secs in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if total >= secs:
            parts.append(f"{total // secs}{unit}")
            total %= secs
        if len(parts) == 2:
            break
    return "".join(parts) or "1s"
